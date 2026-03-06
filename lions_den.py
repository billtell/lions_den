import streamlit as st
import pandas as pd
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheHandler
from typing import Optional
import time
from datetime import date

# ── Must be the very first Streamlit call ──────────────────────────────────────
st.set_page_config(
    page_title="Lion's Den Playlist Generator",
    page_icon="🦁",
    layout="centered",
)

# ── Dark mode CSS ──────────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* Main background */
    .stApp {
        background-color: #0d0d0d;
        color: #e8e8e8;
    }

    /* Sidebar */
    [data-testid="stSidebar"] {
        background-color: #1a1a1a;
    }
    [data-testid="stSidebar"] * {
        color: #e8e8e8 !important;
    }

    /* Headings */
    h1, h2, h3, h4 {
        color: #f5a623 !important;
    }

    /* Info / warning / success banners */
    [data-testid="stAlert"] {
        background-color: #1e1e1e;
        border-radius: 8px;
    }

    /* Buttons */
    .stButton > button {
        background-color: #1db954;
        color: #000000;
        font-weight: 700;
        border: none;
        border-radius: 50px;
        padding: 0.5rem 1.6rem;
        transition: background-color 0.2s ease;
    }
    .stButton > button:hover {
        background-color: #1ed760;
        color: #000000;
    }

    /* Link buttons (Login with Spotify) */
    .stLinkButton > a {
        background-color: #1db954 !important;
        color: #000000 !important;
        font-weight: 700 !important;
        border: none !important;
        border-radius: 50px !important;
        padding: 0.5rem 1.6rem !important;
        text-decoration: none !important;
    }
    .stLinkButton > a:hover {
        background-color: #1ed760 !important;
    }

    /* Text inputs */
    .stTextInput > div > div > input {
        background-color: #1e1e1e;
        color: #e8e8e8;
        border: 1px solid #333;
        border-radius: 6px;
    }

    /* Dataframe / table */
    [data-testid="stDataFrame"] {
        background-color: #1a1a1a;
        border-radius: 8px;
    }

    /* Divider */
    hr {
        border-color: #2a2a2a;
    }

    /* Expander */
    [data-testid="stExpander"] {
        background-color: #1a1a1a;
        border: 1px solid #2a2a2a;
        border-radius: 8px;
    }

    /* Metric labels */
    [data-testid="stMetricLabel"] p {
        color: #aaa !important;
    }
    [data-testid="stMetricValue"] {
        color: #f5a623 !important;
    }
</style>
""", unsafe_allow_html=True)


# ── Custom cache handler (stores token in session state, not on disk) ──────────

class SessionStateCacheHandler(CacheHandler):
    """Stores the Spotify token in st.session_state instead of a local file.
    Required for reliable operation on Streamlit Cloud."""

    def get_cached_token(self):
        return st.session_state.get("spotify_token")

    def save_token_to_cache(self, token_info):
        st.session_state["spotify_token"] = token_info

    def delete_cached_token(self):
        st.session_state.pop("spotify_token", None)


# ── Load app credentials from st.secrets ──────────────────────────────────────

try:
    CLIENT_ID     = st.secrets["SPOTIFY_CLIENT_ID"]
    CLIENT_SECRET = st.secrets["SPOTIFY_CLIENT_SECRET"]
    REDIRECT_URI  = st.secrets.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8501")
except (KeyError, FileNotFoundError):
    st.error(
        "⚠️ Spotify credentials not found. "
        "Add `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, and optionally "
        "`SPOTIFY_REDIRECT_URI` to `.streamlit/secrets.toml`."
    )
    st.stop()

# ── Build the OAuth manager once per session ───────────────────────────────────

if "auth_manager" not in st.session_state:
    st.session_state["auth_manager"] = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope="playlist-modify-public playlist-modify-private",
        cache_handler=SessionStateCacheHandler(),
        show_dialog=True,   # always show the Spotify consent screen
    )

auth_manager: SpotifyOAuth = st.session_state["auth_manager"]

# ── Handle OAuth callback (MUST run before any UI that depends on auth) ────────

code = st.query_params.get("code")
if code and not st.session_state.get("spotify_token"):
    with st.spinner("Completing Spotify login…"):
        try:
            token_info = auth_manager.get_access_token(code, as_dict=True, check_cache=False)
            st.session_state["spotify_token"] = token_info
        except Exception as e:
            st.error(f"Spotify authorization failed: {e}")
            st.stop()
    st.query_params.clear()
    st.rerun()


# ── Token refresh helper ───────────────────────────────────────────────────────

def get_valid_token() -> Optional[dict]:
    """Return a valid (possibly refreshed) token dict, or None if not logged in."""
    token = st.session_state.get("spotify_token")
    if not token:
        return None

    # Scope validation — catches tokens issued before playlist scope was added
    required = {"playlist-modify-public"}
    granted  = set(token.get("scope", "").split())
    if not required.issubset(granted):
        st.warning(
            "⚠️ Your session is missing required Spotify permissions. "
            "Please log out and log in again to grant playlist access."
        )
        st.session_state.pop("spotify_token", None)
        return None

    # Refresh if within 60 seconds of expiry
    if token.get("expires_at", 0) <= time.time() + 60:
        try:
            token = auth_manager.refresh_access_token(token["refresh_token"])
            st.session_state["spotify_token"] = token
        except Exception as e:
            st.warning(f"Token refresh failed — please log in again. ({e})")
            st.session_state.pop("spotify_token", None)
            return None
    return token


# ── CSV loading & deduplication ───────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def load_data(path: str = "lions_den_complete.csv") -> "tuple[pd.DataFrame, int]":
    """Load and deduplicate the track CSV. Returns (clean_df, original_row_count)."""
    df = pd.read_csv(path)
    original_count = len(df)

    df["_artist_norm"] = df["artist"].str.strip().str.lower()
    df["_song_norm"]   = df["song"].str.strip().str.lower()
    df = df.drop_duplicates(subset=["_artist_norm", "_song_norm"])
    df = df.drop(columns=["_artist_norm", "_song_norm"])

    df["artist"] = df["artist"].str.strip()
    df["song"]   = df["song"].str.strip()

    return df.reset_index(drop=True), original_count


# ── Step 1: Pre-screen tracks (search only, no playlist created) ───────────────

def screen_tracks(sp: spotipy.Spotify, selection: pd.DataFrame):
    """Search Spotify for each track and store results in session state."""
    results   = []
    found_uris = []

    total    = len(selection)
    progress = st.progress(0, text="Checking availability on Spotify…")

    for i, (_, row) in enumerate(selection.iterrows()):
        query = f"artist:{row['artist']} track:{row['song']}"
        try:
            res   = sp.search(q=query, limit=1, type="track")
            items = res["tracks"]["items"]
            if items:
                uri = items[0]["uri"]
                found_uris.append(uri)
                results.append({
                    "":       "✅",
                    "Artist": row["artist"],
                    "Song":   row["song"],
                    "Album":  row["album"],
                    "Year":   row["album_year"],
                    "_uri":   uri,
                })
            else:
                results.append({
                    "":       "❌",
                    "Artist": row["artist"],
                    "Song":   row["song"],
                    "Album":  row["album"],
                    "Year":   row["album_year"],
                    "_uri":   None,
                })
        except Exception as e:
            results.append({
                "":       "⚠️",
                "Artist": row["artist"],
                "Song":   row["song"],
                "Album":  row["album"],
                "Year":   row["album_year"],
                "_uri":   None,
            })

        progress.progress((i + 1) / total, text=f"Checking… ({i + 1}/{total})")
        time.sleep(0.1)

    progress.empty()
    st.session_state["screened_results"] = results
    st.session_state["screened_uris"]    = found_uris


# ── Step 2: Create the playlist with pre-screened URIs ────────────────────────

def create_playlist(sp: spotipy.Spotify, found_uris: list, playlist_name: str):
    """Create a Spotify playlist using URIs already found during screening."""
    try:
        new_playlist = sp._post("me/playlists", payload={
            "name":        playlist_name,
            "public":      True,
            "description": "Generated by the Lion's Den Playlist Generator 🦁",
        })
        sp.playlist_add_items(playlist_id=new_playlist["id"], items=found_uris)
        url = new_playlist["external_urls"]["spotify"]
        st.success(f"Playlist created with {len(found_uris)} tracks!")
        st.markdown(f"### [🎧 Open in Spotify]({url})")
        st.session_state["playlist_url"] = url

    except spotipy.SpotifyException as e:
        if e.http_status == 403:
            st.error(
                f"**403 Forbidden** (reason: `{e.reason}`) — Spotify blocked playlist creation.\n\n"
                "**Most likely fix:** Click **Logout** in the sidebar and log back in — your "
                "current session token may be missing playlist permissions.\n\n"
                "**If that doesn't work:** Your Spotify app may still be in Development Mode. "
                "Go to [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) → "
                "your app → **Settings** → **User Management** and confirm your account email "
                "is listed. Changes can take a minute to take effect."
            )
        else:
            st.error(f"Spotify error {e.http_status}: {e.msg} (reason: {e.reason})")
    except Exception as e:
        st.error(f"Unexpected error creating playlist: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# APP LAYOUT
# ══════════════════════════════════════════════════════════════════════════════

st.title("🦁 Lion's Den Playlist Generator")
st.caption("Random mixes from the KDHX Monday Morning Rock Show archive")
st.markdown("---")

token = get_valid_token()

# ── Not logged in ──────────────────────────────────────────────────────────────
if not token:
    st.markdown("### Connect your Spotify account to get started")
    st.markdown(
        "Once connected, you can generate random mixes from the archive and "
        "save them directly to **your** Spotify library."
    )
    st.markdown("")
    auth_url = auth_manager.get_authorize_url()
    st.link_button("🎵 Login with Spotify", auth_url, use_container_width=True)
    st.stop()

# ── Logged in — build Spotify client ──────────────────────────────────────────
sp = spotipy.Spotify(auth=token["access_token"])

# Sidebar: user profile + logout
with st.sidebar:
    try:
        user         = sp.current_user()
        display_name = user.get("display_name") or user.get("id", "Spotify User")
        images       = user.get("images", [])
        if images:
            st.image(images[0]["url"], width=80)
        st.markdown(f"**{display_name}**")
        st.caption(user.get("email", ""))
    except Exception:
        st.markdown("**Connected to Spotify**")

    st.markdown("---")
    if st.button("Logout", use_container_width=True):
        for key in ["spotify_token", "current_mix", "screened_results",
                    "screened_uris", "playlist_url"]:
            st.session_state.pop(key, None)
        st.rerun()

    # ── Diagnostics ──────────────────────────────────────────────────────────
    with st.expander("🔧 Diagnostics"):
        raw_token = st.session_state.get("spotify_token", {})
        granted_scopes = set(raw_token.get("scope", "").split())
        needed_scopes  = {"playlist-modify-public", "playlist-modify-private"}

        st.markdown("**Granted scopes:**")
        for scope in sorted(granted_scopes):
            marker = "✅" if scope in needed_scopes else "•"
            st.markdown(f"{marker} `{scope}`")

        missing = needed_scopes - granted_scopes
        if missing:
            st.error(f"Missing: {', '.join(f'`{s}`' for s in missing)}")
        else:
            st.success("All required scopes granted")

        exp = raw_token.get("expires_at")
        if exp:
            from datetime import datetime
            exp_dt  = datetime.fromtimestamp(exp)
            remains = max(0, int(exp - time.time()))
            st.markdown(f"**Token expires:** {exp_dt.strftime('%H:%M:%S')} ({remains}s remaining)")

        if st.button("🔄 Force re-login", use_container_width=True):
            for key in ["spotify_token", "current_mix", "screened_results",
                        "screened_uris", "playlist_url"]:
                st.session_state.pop(key, None)
            st.rerun()

# ── Track stats ────────────────────────────────────────────────────────────────
with st.spinner("Loading track archive…"):
    df, original_count = load_data()

unique_count = len(df)
removed      = original_count - unique_count

col_a, col_b, col_c = st.columns(3)
col_a.metric("Total entries",      f"{original_count:,}")
col_b.metric("Unique tracks",      f"{unique_count:,}")
col_c.metric("Duplicates removed", f"{removed:,}")

st.markdown("---")

# ── Playlist name ──────────────────────────────────────────────────────────────
default_name  = f"Lion's Den Mix — {date.today().strftime('%B %d, %Y')}"
playlist_name = st.text_input("Playlist name", value=default_name)
st.markdown("")

# ── Generate / Regenerate ──────────────────────────────────────────────────────
if st.button("🎲 Generate Mix", use_container_width=True):
    st.session_state["current_mix"]       = df.sample(10).reset_index(drop=True)
    # Clear any previous screening results when a new mix is generated
    st.session_state.pop("screened_results", None)
    st.session_state.pop("screened_uris",    None)
    st.session_state.pop("playlist_url",     None)

# ── Show current mix ───────────────────────────────────────────────────────────
if st.session_state.get("current_mix") is not None:
    mix = st.session_state["current_mix"]

    screened = st.session_state.get("screened_results")

    if screened:
        # Show results table with availability status
        st.markdown("### Availability Check")
        display_df = pd.DataFrame(screened).drop(columns=["_uri"])
        st.dataframe(display_df, use_container_width=True, hide_index=True)

        found_count = sum(1 for r in screened if r["_uri"])
        not_found   = len(screened) - found_count
        c1, c2 = st.columns(2)
        c1.metric("Available on Spotify", found_count)
        c2.metric("Not available",        not_found)

    else:
        # Show plain mix table before screening
        st.markdown("### Your Mix")
        st.dataframe(
            mix[["artist", "song", "album", "album_year"]].rename(columns={
                "artist":     "Artist",
                "song":       "Song",
                "album":      "Album",
                "album_year": "Year",
            }),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("---")

    found_uris = st.session_state.get("screened_uris")

    if not screened:
        # Step 1 button: check availability
        if st.button("🔍 Check Availability on Spotify", use_container_width=True):
            token = get_valid_token()
            if token:
                sp = spotipy.Spotify(auth=token["access_token"])
                screen_tracks(sp, mix)
                st.rerun()
            else:
                st.error("Session expired — please log in again.")
                st.rerun()

    elif found_uris:
        # Step 2 button: create playlist (only shown after screening finds ≥1 track)
        if st.button(
            f"🎵 Create Playlist with {len(found_uris)} track{'s' if len(found_uris) != 1 else ''}",
            use_container_width=True,
        ):
            token = get_valid_token()
            if token:
                sp = spotipy.Spotify(auth=token["access_token"])
                create_playlist(sp, found_uris, playlist_name)
            else:
                st.error("Session expired — please log in again.")
                st.rerun()

        # Re-screen button in case user wants to retry
        if st.button("🔄 Re-check Availability", use_container_width=True):
            st.session_state.pop("screened_results", None)
            st.session_state.pop("screened_uris",    None)
            st.rerun()

    else:
        st.warning("No tracks from this mix were found on Spotify. Try generating a new mix.")
        if st.button("🔄 Re-check Availability", use_container_width=True):
            st.session_state.pop("screened_results", None)
            st.session_state.pop("screened_uris",    None)
            st.rerun()

    # Persist playlist link across reruns
    if st.session_state.get("playlist_url"):
        st.markdown(f"**Last export:** [Open in Spotify]({st.session_state['playlist_url']})")
