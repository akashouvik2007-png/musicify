import os
import json
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
    """
    response = model.generate_content(gemini_prompt)
    reply = response.text.strip()

    if reply.startswith("```"):
        reply = reply.strip("`")
        if reply.lower().startswith("json"):
            reply = reply[4:].strip()

    data = json.loads(reply)
    return data["playlist_name"], data["songs"]


@app.route("/api/generate", methods=["POST"])
def generate():
    body = request.get_json()
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "No prompt provided"}), 400

    try:
        playlist_name, songs = get_song_list_and_playlist(prompt)
        return jsonify({"playlist_name": playlist_name, "songs": songs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/create-playlist", methods=["POST"])
def create_playlist():
    body = request.get_json()
    access_token = body.get("access_token")
    playlist_name = body.get("playlist_name")
    songs = body.get("songs", [])

    if not access_token or not playlist_name or not songs:
        return jsonify({"error": "Missing required fields"}), 400

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Get user ID
    me = requests.get("https://api.spotify.com/v1/me", headers=headers).json()
    user_id = me.get("id")
    if not user_id:
        return jsonify({"error": "Could not get Spotify user"}), 401

    # Create playlist
    playlist_res = requests.post(
        f"https://api.spotify.com/v1/users/{user_id}/playlists",
        headers=headers,
        json={"name": playlist_name, "public": True}
    ).json()

    playlist_id = playlist_res.get("id")
    playlist_url = playlist_res.get("external_urls", {}).get("spotify")

    if not playlist_id:
        return jsonify({"error": "Could not create playlist"}), 500

    # Search and collect track IDs
    track_ids = []
    not_found = []
    for song in songs:
        query = f"{song['title']} {song['artist']}"
        search = requests.get(
            "https://api.spotify.com/v1/search",
            headers=headers,
            params={"q": query, "type": "track", "limit": 1}
        ).json()
        items = search.get("tracks", {}).get("items", [])
        if items:
            track_ids.append(items[0]["id"])
        else:
            not_found.append(f"{song['title']} by {song['artist']}")

    # Add tracks to playlist
    if track_ids:
        requests.post(
            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
            headers=headers,
            json={"uris": [f"spotify:track:{tid}" for tid in track_ids]}
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
