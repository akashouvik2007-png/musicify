import os
import json
import re
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

app = Flask(__name__)
CORS(app)

# --- Gemini setup ---
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-2.0-flash")


def get_song_list_and_playlist(prompt):
    gemini_prompt = f"""
    Based on this theme: "{prompt}", suggest a playlist.
    Return JSON with this format:
    {{
      "playlist_name": "string",
      "songs": [
        {{"title": "string", "artist": "string"}},
        ...
      ]
    }}
    Make sure there are exactly 15 songs.
    Return ONLY the raw JSON object, no markdown, no backticks, no explanation.
    """
    response = model.generate_content(gemini_prompt)
    reply = response.text.strip()

    # FIX 1: Properly strip markdown code fences using regex
    # Handles ```json ... ``` or ``` ... ``` regardless of whitespace
    reply = re.sub(r"^```(?:json)?\s*", "", reply)
    reply = re.sub(r"\s*```$", "", reply)
    reply = reply.strip()

    try:
        data = json.loads(reply)
    except json.JSONDecodeError as e:
        raise ValueError(f"Gemini returned invalid JSON: {e}\nRaw reply: {reply[:300]}")

    # FIX 2: Validate the response structure
    if "playlist_name" not in data or "songs" not in data:
        raise ValueError("Gemini response missing required fields 'playlist_name' or 'songs'")

    songs = data["songs"]
    if not isinstance(songs, list) or len(songs) == 0:
        raise ValueError("Gemini returned an empty or invalid songs list")

    # Ensure each song has the expected shape
    validated_songs = []
    for s in songs:
        if isinstance(s, dict) and s.get("title") and s.get("artist"):
            validated_songs.append({"title": s["title"], "artist": s["artist"]})

    if not validated_songs:
        raise ValueError("No valid songs found in Gemini response")

    return data["playlist_name"], validated_songs


@app.route("/api/generate", methods=["POST"])
def generate():
    body = request.get_json()
    if not body:
        return jsonify({"error": "Invalid JSON body"}), 400

    prompt = body.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "No prompt provided"}), 400

    try:
        playlist_name, songs = get_song_list_and_playlist(prompt)
        return jsonify({"playlist_name": playlist_name, "songs": songs})
    except ValueError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


@app.route("/api/create-playlist", methods=["POST"])
def create_playlist():
    body = request.get_json()
    if not body:
        return jsonify({"error": "Invalid JSON body"}), 400

    access_token = body.get("access_token")
    playlist_name = body.get("playlist_name")
    songs = body.get("songs", [])

    if not access_token or not playlist_name or not songs:
        return jsonify({"error": "Missing required fields"}), 400

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # FIX 3: Handle Spotify token expiry explicitly
    me_res = requests.get("https://api.spotify.com/v1/me", headers=headers)
    if me_res.status_code == 401:
        return jsonify({"error": "Spotify token expired. Please log in again."}), 401
    if not me_res.ok:
        return jsonify({"error": f"Spotify API error: {me_res.status_code}"}), 502

    me = me_res.json()
    user_id = me.get("id")
    if not user_id:
        return jsonify({"error": "Could not get Spotify user ID"}), 401

    # FIX 4: Search tracks BEFORE creating the playlist to avoid empty orphan playlists
    track_ids = []
    not_found = []

    for song in songs:
        try:
            query = f"{song['title']} {song['artist']}"
            search_res = requests.get(
                "https://api.spotify.com/v1/search",
                headers=headers,
                params={"q": query, "type": "track", "limit": 1},
                timeout=5
            )
            # FIX 5: Handle search errors gracefully per-song instead of crashing
            if not search_res.ok:
                not_found.append(f"{song['title']} by {song['artist']}")
                continue

            items = search_res.json().get("tracks", {}).get("items", [])
            if items:
                track_ids.append(items[0]["id"])
            else:
                not_found.append(f"{song['title']} by {song['artist']}")

        except requests.RequestException:
            not_found.append(f"{song['title']} by {song['artist']}")

    if not track_ids:
        return jsonify({"error": "No tracks could be found on Spotify for this playlist"}), 404

    # Only create the playlist once we know we have tracks
    playlist_res = requests.post(
        f"https://api.spotify.com/v1/users/{user_id}/playlists",
        headers=headers,
        json={"name": playlist_name, "public": True}
    )
    if not playlist_res.ok:
        return jsonify({"error": f"Could not create Spotify playlist: {playlist_res.status_code}"}), 502

    playlist_data = playlist_res.json()
    playlist_id = playlist_data.get("id")
    playlist_url = playlist_data.get("external_urls", {}).get("spotify")

    if not playlist_id:
        return jsonify({"error": "Spotify did not return a playlist ID"}), 500

    # Add tracks (Spotify allows max 100 per request — chunk just in case)
    for i in range(0, len(track_ids), 100):
        chunk = track_ids[i:i + 100]
        requests.post(
            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
            headers=headers,
            json={"uris": [f"spotify:track:{tid}" for tid in chunk]}
        )

    return jsonify({
        "playlist_url": playlist_url,
        "tracks_added": len(track_ids),
        "not_found": not_found
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
