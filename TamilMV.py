import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog
import requests
import re
import os
import sys
import tempfile
import threading
import time
import glob
import libtorrent as lt
import yt_dlp
from PIL import Image
from io import BytesIO
from bs4 import BeautifulSoup
from just_playback import Playback

# Try to import VLC
try:
    import vlc
    HAS_VLC = True
except ImportError:
    HAS_VLC = False
    print("[WARNING] python-vlc is not installed. Video player will be disabled. Run: pip install python-vlc")

# --- Path Logic ---
def get_base_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_base_path()
FFMPEG_EXE = os.path.join(BASE_DIR, "ffmpeg.exe") 
DOWNLOAD_DIR = os.path.join(BASE_DIR, "Downloads")
RESUME_DIR = os.path.join(BASE_DIR, "ResumeData")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(RESUME_DIR, exist_ok=True)

# --- Torrent Engine ---
class TorrentClient:
    def __init__(self, ui_update_callback):
        self.session = lt.session({'listen_interfaces': '0.0.0.0:6881'})
        self.handles = {} 
        self.ui_update_callback = ui_update_callback
        self.running = True
        self.load_resume_data()
        threading.Thread(target=self.monitor_loop, daemon=True).start()

    def add_magnet(self, magnet_uri, movie_title):
        params = lt.parse_magnet_uri(magnet_uri)
        params.save_path = DOWNLOAD_DIR
        handle = self.session.add_torrent(params)
        info_hash = str(handle.info_hash())
        self.handles[info_hash] = {"handle": handle, "title": movie_title}
        return info_hash

    def pause_torrent(self, info_hash):
        if info_hash in self.handles: self.handles[info_hash]["handle"].pause()

    def resume_torrent(self, info_hash):
        if info_hash in self.handles: self.handles[info_hash]["handle"].resume()

    def delete_torrent(self, info_hash, delete_files=False):
        if info_hash in self.handles:
            handle = self.handles[info_hash]["handle"]
            self.session.remove_torrent(handle, delete_files)
            del self.handles[info_hash]
            resume_file = os.path.join(RESUME_DIR, f"{info_hash}.fastresume")
            if os.path.exists(resume_file): os.remove(resume_file)

    def load_resume_data(self):
        for filename in os.listdir(RESUME_DIR):
            if filename.endswith(".fastresume"):
                try:
                    with open(os.path.join(RESUME_DIR, filename), "rb") as f:
                        data = f.read()
                        params = lt.read_resume_data(data)
                        params.save_path = DOWNLOAD_DIR
                        handle = self.session.add_torrent(params)
                        self.handles[str(handle.info_hash())] = {"handle": handle, "title": handle.status().name}
                except: pass

    def save_resume_data(self):
        self.session.pause()
        for h_dict in self.handles.values():
            h = h_dict["handle"]
            if h.is_valid() and h.has_metadata():
                h.save_resume_data(lt.resume_data_flags_t.save_info_dict)
        alerts = self.session.pop_alerts()
        for alert in alerts:
            if isinstance(alert, lt.save_resume_data_alert):
                data = lt.bencode(alert.params)
                info_hash = str(alert.handle.info_hash())
                with open(os.path.join(RESUME_DIR, f"{info_hash}.fastresume"), "wb") as f:
                    f.write(data)

    def monitor_loop(self):
        while self.running:
            for info_hash, data in self.handles.items():
                handle = data["handle"]
                status = handle.status()
                state_str = "Downloading" if status.state == lt.torrent_status.downloading else "Finished" if (status.state == lt.torrent_status.seeding or status.state == lt.torrent_status.finished) else "Paused" if status.paused else "Queued/Metadata"
                stats = {"hash": info_hash, "title": data["title"], "progress": status.progress, "download_rate": status.download_rate / 1000000, "upload_rate": status.upload_rate / 1000000, "peers": status.num_peers, "state": state_str}
                self.ui_update_callback(stats)
            time.sleep(1)

# --- Data Engine ---
class Movie:
    def __init__(self, search_term, year=None, thread_url=None):
        self.search_term, self.year, self.thread_url = search_term, year, thread_url
        self.available_links = [] 
        self.api_key = '7f40a797d6422b73032d61a369f9d044'
        self.title, self.poster_url, self.data_fetched = search_term, None, False
        self.plot, self.rating, self.release_date = "No plot available.", "N/A", "Unknown"
        self.runtime, self.genres, self.cast, self.tagline = "-- min", "Unknown", "Unknown", ""
        self.director, self.production = "Unknown", "Unknown"

    def fetch_data(self):
        if self.data_fetched: return True
        clean_search = self.search_term.strip().replace(" ", "+")
        url = f"https://api.themoviedb.org/3/search/movie?api_key={self.api_key}&query={clean_search}"
        try:
            r = requests.get(url, timeout=5).json()
            results = r.get('results', [])
            if results:
                match = results[0]
                movie_id = match.get('id')
                
                details_url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={self.api_key}&append_to_response=credits"
                details = requests.get(details_url, timeout=5).json()

                self.title = details.get('title', self.title)
                self.tagline = details.get('tagline', "")
                self.plot = details.get('overview', "No overview available.")
                self.rating = str(round(details.get('vote_average', 0), 1))
                self.release_date = details.get('release_date', "Unknown Year")
                
                if details.get('runtime'): self.runtime = f"{details['runtime']} min"
                if details.get('genres'): self.genres = ", ".join([g['name'] for g in details['genres']][:3])
                
                cast_list = details.get('credits', {}).get('cast', [])
                if cast_list: self.cast = ", ".join([c['name'] for c in cast_list[:4]])
                
                crew_list = details.get('credits', {}).get('crew', [])
                directors = [c['name'] for c in crew_list if c['job'] == 'Director']
                if directors: self.director = ", ".join(directors[:2])
                
                prod_list = details.get('production_companies', [])
                if prod_list: self.production = ", ".join([p['name'] for p in prod_list][:2])

                path = details.get('poster_path')
                self.poster_url = f"https://image.tmdb.org/t/p/w500{path}" if path else None
                
            self.data_fetched = True
            return True
        except: return False

# --- Scraper Engine ---
class TamilMV:
    def __init__(self):
        self.domains = ['https://www.1tamilmv.futbol/', 'https://www.1tamilmv.work/']
        self.headers = {"User-Agent": "Mozilla/5.0"}

    def get_latest(self, ui_callback=None):
        for domain in self.domains:
            try:
                response = requests.get(domain, headers=self.headers, timeout=8)
                if response.status_code != 200: continue
                soup = BeautifulSoup(response.text, 'html.parser')
                seen = set()
                containers = soup.find_all('div', class_='banger-container')
                for container in containers:
                    for link in container.find_all('a'):
                        text = link.get_text(separator=" ", strip=True)
                        match = re.search(r"^(.*?)(?:\s*[\(\[](\d{4})[\)\]])", text)
                        if match:
                            name = re.sub(r'[^a-zA-Z0-9\s].*$', '', re.split(r'[-:\[(]', match.group(1))[0]).strip()
                            if name and name not in seen:
                                seen.add(name)
                                m = Movie(name, match.group(2), thread_url=link.get('href'))
                                if ui_callback: ui_callback(m)
                if seen: return
            except: continue

    def get_movie_links(self, thread_url):
        links_data = []
        seen_labels = set()
        try:
            res = requests.get(thread_url, headers=self.headers, timeout=8)
            soup = BeautifulSoup(res.text, 'html.parser')
            
            res_pattern = re.compile(r'(4K|2160p|1080p|1080i|720p|480p|360p|BDRip|Web-DL|WEBRip)', re.IGNORECASE)
            codec_pattern = re.compile(r'(HEVC|AVC|x264|x265|10-bit|TrueHD|DD5\.1|AAC|DTS)', re.IGNORECASE)
            size_pattern = re.compile(r'(\d+(?:\.\d+)?\s*(?:MB|GB|KB))', re.IGNORECASE)
            ep_pattern = re.compile(r'(EP\s*\(?[\d-]+\)?)', re.IGNORECASE)
            
            for a in soup.find_all('a', href=re.compile(r'^magnet:\?')):
                magnet = a['href']
                
                context_text = ""
                for elem in a.previous_elements:
                    if elem.name == 'a' and elem.get('href', '').startswith('magnet:'):
                        break
                    if isinstance(elem, str):
                        context_text = str(elem).strip() + " " + context_text
                    if len(context_text) > 400:
                        break

                res_match = res_pattern.search(context_text)
                codec_matches = codec_pattern.findall(context_text)
                size_match = size_pattern.search(context_text)
                ep_match = ep_pattern.search(context_text)
                
                ep_str = ep_match.group(1).upper() if ep_match else ""
                res_str = res_match.group(1).upper() if res_match else ""
                size_str = size_match.group(1).upper() if size_match else ""
                codec_str = " ".join(sorted(set(codec_matches))).upper() if codec_matches else ""

                label_parts = filter(None, [ep_str, res_str, codec_str, size_str])
                label = " | ".join(label_parts)
                
                if not label: label = "Magnet Link"
                
                original_label = label
                counter = 1
                while label in seen_labels:
                    label = f"{original_label} (Alt {counter})"
                    counter += 1
                
                seen_labels.add(label)
                links_data.append({"label": label, "magnet": magnet})
        except Exception as e: pass
        
        return sorted(links_data, key=lambda x: ("4K" not in x["label"], "1080P" not in x["label"]))

# --- Main GUI Application ---
class MovieApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("TamilMV Ultra")
        self.geometry("1400x900")
        ctk.set_appearance_mode("dark") 

        self.scraper, self.playback = TamilMV(), Playback()
        self.torrent_client = TorrentClient(self.update_torrent_ui)
        self.last_volume, self.all_movies, self.sidebar_buttons = 0.7, [], {}
        self.current_download_hash, self.search_timer = None, None
        self.is_muted = False
        self.health_scanning = False
        
        self.grid_columnconfigure(1, weight=1)
        self.grid_columnconfigure(2, weight=0)
        self.grid_rowconfigure(0, weight=1)

        # ==================== 1. SIDEBAR ====================
        self.sidebar = ctk.CTkFrame(self, width=300, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        ctk.CTkLabel(self.sidebar, text="TamilMV", font=("Arial", 18, "bold")).pack(pady=20)
        self.search_entry = ctk.CTkEntry(self.sidebar, placeholder_text="Live search...")
        self.search_entry.pack(padx=20, pady=(0, 15), fill="x")
        self.search_entry.bind("<KeyRelease>", self.schedule_filter)
        
        self.stream_btn = ctk.CTkButton(self.sidebar, text="🎬 Discover", anchor="w", fg_color="gray25", command=lambda: self.switch_tab("stream"))
        self.stream_btn.pack(padx=20, pady=5, fill="x")
        self.down_btn = ctk.CTkButton(self.sidebar, text="📥 Torrent Manager", anchor="w", fg_color="transparent", command=lambda: self.switch_tab("download"))
        self.down_btn.pack(padx=20, pady=5, fill="x")
        
        self.player_tab_btn = ctk.CTkButton(self.sidebar, text="🍿 Video Player", anchor="w", fg_color="transparent", command=lambda: self.switch_tab("player"))
        self.player_tab_btn.pack(padx=20, pady=5, fill="x")

        self.scroll = ctk.CTkScrollableFrame(self.sidebar, label_text="Live Feed", fg_color="transparent")
        self.scroll.pack(fill="both", expand=True, padx=10, pady=10)

        # ==================== 2. MAIN CONTAINER ====================
        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.container.grid_columnconfigure(0, weight=1)
        self.container.grid_rowconfigure(0, weight=1)

        # --- TAB 1: DISCOVER ---
        self.stream_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        self.stream_frame.grid(row=0, column=0, sticky="nsew")
        
        # Configure layout to allow the Health panel to span both columns
        self.stream_frame.grid_columnconfigure(1, weight=1) 
        self.stream_frame.grid_rowconfigure(0, weight=1) # Upper section (Poster + Meta) expands
        self.stream_frame.grid_rowconfigure(2, weight=0) # Lower section (Health Panel) stays fixed
        
        # Left Side: Poster
        self.poster_display = ctk.CTkLabel(self.stream_frame, text="Select a movie from the Navigator", width=350, height=520, fg_color="gray15", corner_radius=10)
        self.poster_display.grid(row=0, column=0, padx=(0, 30), pady=10, sticky="nw")
        
        # Right Side: Metadata
        self.metadata_frame = ctk.CTkFrame(self.stream_frame, fg_color="transparent")
        self.metadata_frame.grid(row=0, column=1, sticky="nsew", pady=10)
        
        self.action_frame = ctk.CTkFrame(self.metadata_frame, fg_color="transparent")
        self.action_frame.pack(side="bottom", anchor="w", fill="x", pady=(10, 0))
        
        self.quality_var = ctk.StringVar(value="Awaiting Selection...")
        self.quality_dropdown = ctk.CTkOptionMenu(self.action_frame, variable=self.quality_var, values=["Awaiting Selection..."], state="disabled", width=600, height=45, font=("Arial", 15))
        self.quality_dropdown.pack(anchor="w", pady=(0, 10))
        
        self.btn_row = ctk.CTkFrame(self.action_frame, fg_color="transparent")
        self.btn_row.pack(anchor="w", fill="x")
        
        self.download_action_btn = ctk.CTkButton(self.btn_row, text="📥 Download to Engine", font=("Arial", 16, "bold"), height=45, width=295, state="disabled")
        self.download_action_btn.pack(side="left", padx=(0, 10))

        self.trailer_btn = ctk.CTkButton(self.btn_row, text="▶ Watch Trailer", font=("Arial", 16, "bold"), height=45, width=295, fg_color="#C0392B", hover_color="#922B21", state="disabled")
        self.trailer_btn.pack(side="left")

        self.meta_title = ctk.CTkLabel(self.metadata_frame, text="Title", font=("Arial", 36, "bold"), wraplength=650, justify="left")
        self.meta_title.pack(anchor="w", pady=(0, 2))
        
        self.meta_tagline = ctk.CTkLabel(self.metadata_frame, text="Tagline", font=("Arial", 16, "italic"), text_color="gray50")
        self.meta_tagline.pack(anchor="w", pady=(0, 15))

        self.meta_stats = ctk.CTkLabel(self.metadata_frame, text="Year: --  |  Runtime: --  |  Rating: --/10", font=("Arial", 16, "bold"), text_color="#3498DB")
        self.meta_stats.pack(anchor="w", pady=(0, 15))
        
        self.meta_genres = ctk.CTkLabel(self.metadata_frame, text="Genres: --", font=("Arial", 15), text_color="gray70")
        self.meta_genres.pack(anchor="w", pady=(0, 5))
        
        self.meta_director = ctk.CTkLabel(self.metadata_frame, text="Director: --", font=("Arial", 15), text_color="gray70")
        self.meta_director.pack(anchor="w", pady=(0, 5))

        self.meta_production = ctk.CTkLabel(self.metadata_frame, text="Studios: --", font=("Arial", 15), text_color="gray70")
        self.meta_production.pack(anchor="w", pady=(0, 5))

        self.meta_cast = ctk.CTkLabel(self.metadata_frame, text="Cast: --", font=("Arial", 15), text_color="gray70")
        self.meta_cast.pack(anchor="w", pady=(0, 15))
        
        self.meta_plot = ctk.CTkLabel(self.metadata_frame, text="Plot overview will appear here...", font=("Arial", 16), wraplength=600, justify="left", text_color="gray85")
        self.meta_plot.pack(anchor="w", pady=(0, 15))

        # --- NEW: Bottom Health Scanner Area (Spans both left and right columns) ---
        self.health_title = ctk.CTkLabel(self.stream_frame, text="Live Torrent Health Scanner", font=("Arial", 18, "bold"))
        self.health_title.grid(row=1, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 0))

        self.health_frame = ctk.CTkScrollableFrame(self.stream_frame, height=180, fg_color="gray10", corner_radius=10)
        self.health_frame.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=10, pady=(5, 10))
        self.health_widgets = {}

        # --- TAB 2: DOWNLOADS ---
        self.download_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        self.download_frame.grid_columnconfigure(0, weight=1)
        self.download_frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(self.download_frame, text="Libtorrent Control Center", font=("Arial", 22, "bold")).grid(row=0, column=0, sticky="w", pady=(0,10))

        self.debug_console = ctk.CTkTextbox(self.download_frame, font=("Consolas", 13), fg_color="gray10", text_color="#00FF00")
        self.debug_console.grid(row=1, column=0, sticky="nsew", pady=10)
        self.log_to_console("[System] libtorrent engine initialized. Dedicated frame active.")

        self.active_dl_frame = ctk.CTkFrame(self.download_frame)
        self.active_dl_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0), ipadx=10, ipady=10)
        
        self.dl_title_label = ctk.CTkLabel(self.active_dl_frame, text="No active downloads", font=("Arial", 16, "bold"))
        self.dl_title_label.pack(anchor="w", padx=10, pady=(10, 5))
        
        self.progress_bar = ctk.CTkProgressBar(self.active_dl_frame, height=12)
        self.progress_bar.pack(fill="x", padx=10, pady=10)
        self.progress_bar.set(0)
        
        self.dl_stats_label = ctk.CTkLabel(self.active_dl_frame, text="Waiting for engine...", font=("Arial", 13), text_color="gray70")
        self.dl_stats_label.pack(anchor="w", padx=10)

        self.dl_controls = ctk.CTkFrame(self.active_dl_frame, fg_color="transparent")
        self.dl_controls.pack(fill="x", padx=10, pady=(15, 10))
        ctk.CTkButton(self.dl_controls, text="▶ Resume", width=90, fg_color="gray30", command=self.res_current).pack(side="left", padx=(0, 5))
        ctk.CTkButton(self.dl_controls, text="⏸ Pause", width=90, fg_color="gray30", command=self.pause_current).pack(side="left", padx=5)
        ctk.CTkButton(self.dl_controls, text="📂 Open Folder", width=100, fg_color="gray30", command=self.open_dir).pack(side="left", padx=5)
        ctk.CTkButton(self.dl_controls, text="🗑 Delete", width=90, fg_color="#C0392B", hover_color="#922B21", command=self.del_current).pack(side="right")
        self.delete_torrent_file_checkbox = ctk.CTkCheckBox(self.dl_controls, text="Delete Data", checkbox_width=18, checkbox_height=18)
        self.delete_torrent_file_checkbox.pack(side="right", padx=15)

        # --- TAB 3: VIDEO PLAYER ---
        self.player_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        self.player_frame.grid_columnconfigure(0, weight=1)
        self.player_frame.grid_rowconfigure(0, weight=1) 
        
        self.video_surface = ctk.CTkFrame(self.player_frame, fg_color="black", corner_radius=0)
        self.video_surface.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        self.video_surface.bind("<Button-3>", self.show_vlc_menu)

        self.vid_controls = ctk.CTkFrame(self.player_frame, height=80)
        self.vid_controls.grid(row=1, column=0, sticky="ew")
        
        self.seek_var = ctk.DoubleVar()
        self.seek_slider = ctk.CTkSlider(self.vid_controls, variable=self.seek_var, command=self.seek_video)
        self.seek_slider.pack(fill="x", padx=20, pady=(10, 5))
        
        self.vid_btn_frame = ctk.CTkFrame(self.vid_controls, fg_color="transparent")
        self.vid_btn_frame.pack(fill="x", padx=10, pady=5)
        
        self.vid_left_btns = ctk.CTkFrame(self.vid_btn_frame, fg_color="transparent")
        self.vid_left_btns.pack(side="left")
        
        ctk.CTkButton(self.vid_left_btns, text="📂 Open File", width=100, command=self.open_video_file).pack(side="left", padx=5)
        ctk.CTkButton(self.vid_left_btns, text="⏪ 10s", width=50, command=lambda: self.skip_video(-10)).pack(side="left", padx=5)
        ctk.CTkButton(self.vid_left_btns, text="▶", width=50, command=self.play_video).pack(side="left", padx=5)
        ctk.CTkButton(self.vid_left_btns, text="⏸", width=50, command=self.pause_video).pack(side="left", padx=5)
        ctk.CTkButton(self.vid_left_btns, text="⏹", width=50, command=self.stop_video).pack(side="left", padx=5)
        ctk.CTkButton(self.vid_left_btns, text="10s ⏩", width=50, command=lambda: self.skip_video(10)).pack(side="left", padx=5)
        ctk.CTkButton(self.vid_left_btns, text="💬 Menu", width=80, fg_color="gray30", command=lambda: self.show_vlc_menu(None, force=True)).pack(side="left", padx=15)

        self.vid_right_btns = ctk.CTkFrame(self.vid_btn_frame, fg_color="transparent")
        self.vid_right_btns.pack(side="right")
        
        ctk.CTkLabel(self.vid_right_btns, text="🔊", font=("Arial", 16)).pack(side="left", padx=5)
        self.vid_vol_slider = ctk.CTkSlider(self.vid_right_btns, from_=0, to=100, width=120, command=self.set_video_vol)
        self.vid_vol_slider.pack(side="left", padx=5)
        self.vid_vol_slider.set(100)

        self.vlc_menu = tk.Menu(self, tearoff=0)
        self.init_vlc()

        # ==================== 3. AUDIO PANEL ====================
        self.audio_panel = ctk.CTkFrame(self, width=80, corner_radius=0)
        self.audio_panel.grid(row=0, column=2, sticky="nsew")
        
        ctk.CTkLabel(self.audio_panel, text="AUDIO", font=("Arial", 12, "bold")).pack(pady=(30, 10))
        self.play_btn = ctk.CTkButton(self.audio_panel, text="⏸", width=50, height=40, font=("Arial", 18), command=self.toggle_audio)
        self.play_btn.pack(pady=10)
        
        self.mute_btn = ctk.CTkButton(self.audio_panel, text="🔊", width=50, height=40, font=("Arial", 18), fg_color="gray30", command=self.toggle_mute)
        self.mute_btn.pack(pady=10)
        
        self.vol_slider = ctk.CTkSlider(self.audio_panel, orientation="vertical", from_=0, to=1, command=self.set_vol, height=250)
        self.vol_slider.pack(expand=True, pady=30)
        self.vol_slider.set(self.last_volume)

        threading.Thread(target=self.live_scrape_thread, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    # --- UI Updaters ---
    def switch_tab(self, tab):
        self.stream_frame.grid_forget()
        self.download_frame.grid_forget()
        self.player_frame.grid_forget() 
        
        self.stream_btn.configure(fg_color="transparent")
        self.down_btn.configure(fg_color="transparent")
        self.player_tab_btn.configure(fg_color="transparent")
        
        if tab == "stream":
            self.stream_frame.grid(row=0, column=0, sticky="nsew")
            self.stream_btn.configure(fg_color="gray25")
        elif tab == "download":
            self.download_frame.grid(row=0, column=0, sticky="nsew")
            self.down_btn.configure(fg_color="gray25")
        elif tab == "player":
            self.player_frame.grid(row=0, column=0, sticky="nsew")
            self.player_tab_btn.configure(fg_color="gray25")
            self.after(100, self.attach_vlc) 
            
            if self.playback.active and self.playback.playing:
                self.playback.pause()
                self.play_btn.configure(text="▶")

    def log_to_console(self, text):
        self.debug_console.configure(state="normal")
        self.debug_console.insert("end", f"{text}\n")
        self.debug_console.see("end")
        self.debug_console.configure(state="disabled")

    def update_torrent_ui(self, stats):
        if self.current_download_hash == stats["hash"]:
            self.after(0, lambda: self.progress_bar.set(stats["progress"]))
            status_text = f"{stats['state']}  |  {stats['progress'] * 100:.1f}%  |  Peers: {stats['peers']}  |  ↓ {stats['download_rate']:.1f} MB/s"
            self.after(0, lambda: self.dl_stats_label.configure(text=status_text))

    def live_scrape_thread(self): self.scraper.get_latest(ui_callback=self.add_movie_to_ui)

    def add_movie_to_ui(self, movie):
        self.all_movies.append(movie)
        self.after(0, lambda: self.create_sidebar_btn(movie))

    def create_sidebar_btn(self, movie):
        btn = ctk.CTkButton(self.scroll, text=movie.title, anchor="w", fg_color="transparent", command=lambda obj=movie: self.select_movie(obj))
        btn.pack(fill="x", pady=2); self.sidebar_buttons[movie.title] = btn 

    def schedule_filter(self, e=None):
        if self.search_timer: self.after_cancel(self.search_timer)
        self.search_timer = self.after(300, self.apply_filter)

    def apply_filter(self):
        query = self.search_entry.get().lower()
        for w in self.scroll.winfo_children(): w.destroy()
        for m in (self.all_movies if not query else [m for m in self.all_movies if query in m.title.lower()]): self.create_sidebar_btn(m)

    def select_movie(self, movie):
        # Stop any existing health scan safely
        self.health_scanning = False 
        
        self.poster_display.configure(image=None, text="Fetching Data & Links...")
        self.meta_title.configure(text=movie.title)
        self.meta_tagline.configure(text="Loading...")
        self.meta_plot.configure(text="Loading metadata...")
        self.quality_dropdown.configure(state="disabled", values=["Scanning Forum..."])
        self.quality_var.set("Scanning Forum...")
        self.download_action_btn.configure(state="disabled", text="📥 Download to Engine")
        self.trailer_btn.configure(state="disabled", text="▶ Watch Trailer", command=lambda: self.watch_trailer(movie.title))
        
        # Clear health UI
        for widget in self.health_frame.winfo_children():
            widget.destroy()
        self.health_widgets = {}
        
        self.playback.stop()
        self.play_btn.configure(text="⏸")
        
        def process():
            movie.fetch_data()
            self.after(0, lambda: self.meta_title.configure(text=movie.title))
            self.after(0, lambda: self.meta_tagline.configure(text=f'"{movie.tagline}"' if movie.tagline else ""))
            self.after(0, lambda: self.meta_stats.configure(text=f"Year: {movie.release_date[:4]}  |  Runtime: {movie.runtime}  |  Rating: {movie.rating}/10"))
            self.after(0, lambda: self.meta_genres.configure(text=f"Genres: {movie.genres}"))
            self.after(0, lambda: self.meta_director.configure(text=f"Director: {movie.director}"))
            self.after(0, lambda: self.meta_production.configure(text=f"Studios: {movie.production}"))
            self.after(0, lambda: self.meta_cast.configure(text=f"Cast: {movie.cast}"))
            self.after(0, lambda: self.meta_plot.configure(text=movie.plot))
            
            if movie.poster_url:
                try:
                    raw_data = requests.get(movie.poster_url, timeout=5).content
                    pil_img = Image.open(BytesIO(raw_data))
                    def apply_poster():
                        img = ctk.CTkImage(pil_img, size=(350, 520))
                        self.poster_display.configure(image=img, text="")
                        self.poster_display.image = img
                    self.after(0, apply_poster)
                except Exception as e: 
                    self.after(0, lambda: self.poster_display.configure(image=None, text="Image Load Failed"))
            else: 
                self.after(0, lambda: self.poster_display.configure(image=None, text="No Poster Available"))
                
            self.after(0, lambda: self.trailer_btn.configure(state="normal"))
            
            links = self.scraper.get_movie_links(movie.thread_url)
            movie.available_links = links
            if links:
                opts = [l["label"] for l in links]
                self.after(0, lambda: self.quality_dropdown.configure(state="normal", values=opts))
                self.after(0, lambda: self.quality_var.set(opts[0]))
                self.after(0, lambda: self.download_action_btn.configure(state="normal", command=lambda: self.start_dl(movie)))
                
                # --- NEW: Build and Run Health Scanner ---
                self.after(0, lambda: self.build_health_ui(links, movie.title))
                threading.Thread(target=self.run_health_scan, args=(links,), daemon=True).start()
            else:
                self.after(0, lambda: self.quality_var.set("No Torrents Found"))
            
            self.start_music(movie.title)
        threading.Thread(target=process, daemon=True).start()

    # --- NEW: Health Scanner Engine ---
    def build_health_ui(self, links, movie_title):
        for link in links:
            row = ctk.CTkFrame(self.health_frame, fg_color="gray20", corner_radius=5)
            row.pack(fill="x", pady=3, padx=5)
            
            lbl_name = ctk.CTkLabel(row, text=link['label'], width=380, anchor="w", font=("Arial", 14, "bold"))
            lbl_name.pack(side="left", padx=10, pady=5)
            
            lbl_peers = ctk.CTkLabel(row, text="Peers: --  |  Seeds: --", width=180, anchor="w", font=("Arial", 13), text_color="gray60")
            lbl_peers.pack(side="left", padx=10)
            
            lbl_status = ctk.CTkLabel(row, text="🔄 Connecting (DHT)...", width=180, anchor="w", font=("Arial", 13, "bold"), text_color="#3498DB")
            lbl_status.pack(side="left", padx=10)
            
            btn_dl = ctk.CTkButton(row, text="📥 Quick DL", width=100, height=28, font=("Arial", 12, "bold"), command=lambda l=link: self.quick_dl(movie_title, l))
            btn_dl.pack(side="right", padx=10)
            
            self.health_widgets[link['label']] = {"peers": lbl_peers, "status": lbl_status}

    def run_health_scan(self, links):
        self.health_scanning = True
        try:
            # Independent session to prevent polluting the main downloads
            temp_session = lt.session({'listen_interfaces': '0.0.0.0:0'}) 
            handles = {}
            for link in links:
                try:
                    p = lt.parse_magnet_uri(link['magnet'])
                    p.save_path = tempfile.gettempdir()
                    h = temp_session.add_torrent(p)
                    handles[link['label']] = h
                except: pass
            
            # Poll for 30 seconds
            for _ in range(30):
                if not getattr(self, "health_scanning", False): break
                
                for label, h in handles.items():
                    s = h.status()
                    peers = s.num_peers
                    seeds = s.num_seeds
                    has_meta = h.has_metadata()
                    
                    if seeds > 5 or peers > 20:
                        state, color = "🟢 Excellent Health", "#2ECC71"
                    elif seeds > 0 or peers > 2:
                        state, color = "🟡 Moderate Health", "#F1C40F"
                    elif has_meta:
                        state, color = "🔴 Low Health", "#E74C3C"
                    else:
                        state, color = "🔄 Connecting (DHT)...", "#3498DB"
                    
                    if label in self.health_widgets:
                        w = self.health_widgets[label]
                        self.after(0, lambda w=w, p=peers, s=seeds, st=state, c=color: self.update_health_row(w, p, s, st, c))
                time.sleep(1)
            
            # Safe cleanup
            del temp_session
        except Exception as e:
            print(f"Health scan error: {e}")
        finally:
            self.health_scanning = False

    def update_health_row(self, w, p, s, st, c):
        try:
            w["peers"].configure(text=f"Peers: {p}  |  Seeds: {s}")
            w["status"].configure(text=st, text_color=c)
        except: pass

    def quick_dl(self, movie_title, link):
        short_label = link["label"].split('|')[-1].strip() if '|' in link["label"] else "DL"
        display_name = f"{movie_title} [{short_label}]"
        
        self.current_download_hash = self.torrent_client.add_magnet(link["magnet"], display_name)
        self.dl_title_label.configure(text=display_name)
        self.log_to_console(f"[+] Quick Added to Engine: {display_name}")
        self.download_action_btn.configure(text="Sent to Torrent Manager! ✔️")

    # --- Standard Download Route ---
    def start_dl(self, movie):
        target = next((l["magnet"] for l in movie.available_links if l["label"] == self.quality_var.get()), None)
        if target:
            short_label = self.quality_var.get().split('|')[-1].strip() if '|' in self.quality_var.get() else "DL"
            display_name = f"{movie.title} [{short_label}]"
            
            self.current_download_hash = self.torrent_client.add_magnet(target, display_name)
            self.dl_title_label.configure(text=display_name)
            self.log_to_console(f"[+] Added to Engine: {display_name}")
            self.download_action_btn.configure(text="Sent to Torrent Manager! ✔️")

    def watch_trailer(self, title):
        if not HAS_VLC: 
            self.log_to_console("[System] VLC not installed. Cannot play trailer.")
            return

        def stream():
            self.after(0, lambda: self.trailer_btn.configure(text="Loading Trailer...", state="disabled"))
            try:
                opts = {
                    'format': 'best', 
                    'quiet': True,
                    'extractor_args': {'youtube': {'player_client': ['android', 'ios', 'web']}}
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(f"ytsearch1:{title} official movie trailer", download=False)
                    url = info['entries'][0]['url']
                
                self.after(0, lambda: self.switch_tab("player"))
                if self.playback.active and self.playback.playing:
                    self.after(0, self.toggle_audio)
                
                media = self.vlc_instance.media_new(url)
                self.media_player.set_media(media)
                self.after(100, self.attach_vlc)
                self.after(500, self.media_player.play)
                
                self.after(0, lambda: self.trailer_btn.configure(text="▶ Watch Trailer", state="normal"))
            except Exception as e:
                print(f"Trailer Error: {e}")
                self.after(0, lambda: self.trailer_btn.configure(text="Trailer Failed", state="normal"))
                
        threading.Thread(target=stream, daemon=True).start()

    # --- VLC Player Engine ---
    def init_vlc(self):
        if not HAS_VLC: return
        self.vlc_instance = vlc.Instance('--no-xlib')
        self.media_player = self.vlc_instance.media_player_new()
        self.update_seekbar_loop()

    def attach_vlc(self):
        if not HAS_VLC: return
        self.update() 
        window_id = self.video_surface.winfo_id()
        if sys.platform == "win32":
            self.media_player.set_hwnd(window_id)
        elif sys.platform.startswith("linux"):
            self.media_player.set_xwindow(window_id)
        elif sys.platform == "darwin":
            self.media_player.set_nsobject(window_id)

    def open_video_file(self):
        if not HAS_VLC: return
        filepath = filedialog.askopenfilename(initialdir=DOWNLOAD_DIR, title="Select Movie", filetypes=(("Video Files", "*.mp4 *.mkv *.avi *.webm"), ("All Files", "*.*")))
        if filepath:
            if self.playback.active and self.playback.playing:
                self.toggle_audio()
            
            media = self.vlc_instance.media_new(filepath)
            self.media_player.set_media(media)
            self.attach_vlc()
            self.media_player.play()

    def play_video(self):
        if HAS_VLC: self.media_player.play()
        
    def pause_video(self):
        if HAS_VLC: self.media_player.pause()
        
    def stop_video(self):
        if HAS_VLC: self.media_player.stop()

    def skip_video(self, delta):
        if HAS_VLC:
            curr_time = self.media_player.get_time()
            if curr_time > -1:
                self.media_player.set_time(curr_time + (delta * 1000))

    def seek_video(self, value):
        if HAS_VLC:
            self.media_player.set_position(float(value))

    def set_video_vol(self, value):
        if HAS_VLC and self.media_player:
            self.media_player.audio_set_volume(int(value))

    def update_seekbar_loop(self):
        if HAS_VLC and self.media_player.is_playing():
            pos = self.media_player.get_position()
            if pos >= 0:
                self.seek_var.set(pos)
        self.after(1000, self.update_seekbar_loop)

    def show_vlc_menu(self, event, force=False):
        if not HAS_VLC: return
        if not self.media_player.get_media(): return 

        self.vlc_menu.delete(0, "end")
        
        sub_menu = tk.Menu(self.vlc_menu, tearoff=0)
        spu_tracks = self.media_player.video_get_spu_description()
        if spu_tracks:
            for track_id, track_name in spu_tracks:
                name_str = track_name.decode('utf-8') if isinstance(track_name, bytes) else track_name
                sub_menu.add_command(label=name_str, command=lambda t_id=track_id: self.media_player.video_set_spu(t_id))
        self.vlc_menu.add_cascade(label="💬 Subtitles", menu=sub_menu)

        aud_menu = tk.Menu(self.vlc_menu, tearoff=0)
        aud_tracks = self.media_player.audio_get_track_description()
        if aud_tracks:
            for track_id, track_name in aud_tracks:
                name_str = track_name.decode('utf-8') if isinstance(track_name, bytes) else track_name
                aud_menu.add_command(label=name_str, command=lambda t_id=track_id: self.media_player.audio_set_track(t_id))
        self.vlc_menu.add_cascade(label="🔊 Audio Tracks", menu=aud_menu)

        if force: 
            x = self.winfo_rootx() + self.winfo_width() // 2
            y = self.winfo_rooty() + self.winfo_height() // 2
        else:     
            x, y = event.x_root, event.y_root
            
        self.vlc_menu.tk_popup(x, y)

    # --- Torrent Controls ---
    def res_current(self):
        if self.current_download_hash: self.torrent_client.resume_torrent(self.current_download_hash)
    def pause_current(self):
        if self.current_download_hash: self.torrent_client.pause_torrent(self.current_download_hash)
    def open_dir(self): os.startfile(DOWNLOAD_DIR)
    def del_current(self):
        if self.current_download_hash: 
            self.torrent_client.delete_torrent(self.current_download_hash, self.delete_torrent_file_checkbox.get() == 1)
            self.dl_title_label.configure(text="No active downloads"); self.progress_bar.set(0)
            self.dl_stats_label.configure(text="Deleted."); self.current_download_hash = None

    # --- Background Audio Controls ---
    def start_music(self, title):
        if self.playback.active:
            self.playback.stop()

        clean_name = re.sub(r'\W+', '', title)
        base_path = os.path.join(tempfile.gettempdir(), f"m_{clean_name}")
        mp3_path = base_path + ".mp3"
        m4a_path = base_path + ".m4a"

        has_ffmpeg = os.path.exists(FFMPEG_EXE)

        if has_ffmpeg:
            target_path = mp3_path
            opts = {
                'format': 'bestaudio/best',
                'outtmpl': base_path + '.%(ext)s',
                'quiet': True,
                'overwrites': True,
                'extractor_args': {'youtube': {'player_client': ['android', 'ios', 'web']}},
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '128'}],
                'ffmpeg_location': FFMPEG_EXE
            }
        else:
            target_path = m4a_path
            opts = {
                'format': '140', 
                'outtmpl': m4a_path,
                'quiet': True,
                'overwrites': True,
                'extractor_args': {'youtube': {'player_client': ['android', 'ios', 'web']}}
            }

        try:
            if not os.path.exists(target_path):
                for f in glob.glob(base_path + ".*"):
                    try: os.remove(f)
                    except: pass

                with yt_dlp.YoutubeDL(opts) as ydl: 
                    ydl.download([f"ytsearch1:{title} official theme song bgm"])
            
            if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                self.playback.load_file(target_path)
                self.playback.play()
                self.playback.set_volume(self.last_volume)
                self.after(0, lambda: self.play_btn.configure(text="⏸"))
            else:
                self.after(0, lambda: self.log_to_console("[System] Audio Failed: File is missing or empty."))
        except Exception as e: 
            self.after(0, lambda: self.log_to_console(f"[System] Audio Pipeline Error: {e}"))

    def toggle_audio(self):
        if self.playback.playing: self.playback.pause(); self.play_btn.configure(text="▶")
        else: self.playback.resume(); self.play_btn.configure(text="⏸")

    def toggle_mute(self):
        if self.is_muted:
            self.playback.set_volume(self.last_volume)
            self.mute_btn.configure(text="🔊", fg_color="gray30")
            self.is_muted = False
        else:
            self.playback.set_volume(0)
            self.mute_btn.configure(text="🔇", fg_color="#C0392B")
            self.is_muted = True

    def set_vol(self, v): 
        if not self.is_muted: 
            self.last_volume = float(v)
            self.playback.set_volume(self.last_volume)

    def on_closing(self):
        self.health_scanning = False # Stop tracker ping loop
        self.playback.stop()
        if HAS_VLC and self.media_player: self.media_player.stop()
        self.torrent_client.running = False
        self.torrent_client.save_resume_data()
        self.destroy()

if __name__ == "__main__": MovieApp().mainloop()