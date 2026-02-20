#!/usr/bin/env python3
"""
Discord Music Bot + Flask Web Panel (Mobile PWA)
-------------------------------------------------
Создай .env файл рядом с bot.py:
    DISCORD_TOKEN=твой_токен
Запуск:
    python bot.py
"""

import subprocess, sys, os, shutil, random, audioop

REQUIRED = [
    "discord.py[voice]",
    "flask",
    "flask-cors",
    "yt-dlp",
    "mutagen",
    "python-dotenv",
    "PyNaCl",
    "gTTS",
]

def install_deps():
    print("[*] Проверка зависимостей...")
    for pkg in REQUIRED:
        imp = pkg.split("[")[0].replace("-","_").replace(".","_")
        try:
            __import__(imp)
        except ImportError:
            print(f"  -> Устанавливаю {pkg}...")
            subprocess.check_call([sys.executable,"-m","pip","install",pkg,"--quiet"])
    if not shutil.which("ffmpeg"):
        print("[!] ffmpeg не найден в PATH. Установи ffmpeg вручную.")
        print("    Windows: winget install ffmpeg / choco install ffmpeg")
        print("    Linux:   sudo apt install ffmpeg")
    print("[+] Готово.\n")

install_deps()

import discord
from discord.ext import commands
import yt_dlp, asyncio, threading, json, uuid, time
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from mutagen import File as MutaFile
from gtts import gTTS
from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN","")
if not DISCORD_TOKEN:
    print("[!] Создай .env файл с DISCORD_TOKEN=твой_токен")
    sys.exit(1)

UPLOAD_FOLDER = "uploads"
LIBRARY_FILE  = "library.json"
CONFIG_FILE   = "config.json"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE,"r",encoding="utf-8") as f: return json.load(f)
    return {}

def save_config(cfg):
    with open(CONFIG_FILE,"w",encoding="utf-8") as f: json.dump(cfg,f,ensure_ascii=False,indent=2)

def load_library():
    if os.path.exists(LIBRARY_FILE):
        with open(LIBRARY_FILE,"r",encoding="utf-8") as f: return json.load(f)
    return []

def save_library(lib):
    with open(LIBRARY_FILE,"w",encoding="utf-8") as f: json.dump(lib,f,ensure_ascii=False,indent=2)

# ── DISCORD ──────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="/", intents=intents)
player_state = {}

YDL_STREAM = {"format":"bestaudio/best","quiet":True,"no_warnings":True,"source_address":"0.0.0.0","noplaylist":True}
FFMPEG_OPTS = {"before_options":"-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5","options":"-vn"}

class OverlayAudioSource(discord.AudioSource):
    """Mixes short TTS over an already playing PCM source without restarting track."""
    def __init__(self, primary, overlay=None, duck=0.45, overlay_gain=1.35):
        self.primary = primary
        self.overlay = overlay
        self.duck = duck
        self.overlay_gain = overlay_gain

    def is_opus(self):
        return False

    def add_overlay(self, overlay):
        self.overlay = overlay

    @property
    def volume(self):
        return getattr(self.primary, 'volume', 1.0)

    @volume.setter
    def volume(self, v):
        if hasattr(self.primary, 'volume'):
            self.primary.volume = v

    def cleanup(self):
        try:
            if self.overlay:
                self.overlay.cleanup()
        except Exception:
            pass
        try:
            if self.primary:
                self.primary.cleanup()
        except Exception:
            pass

    def _pad(self, data, size):
        if not data:
            return b"\x00" * size
        if len(data) < size:
            return data + (b"\x00" * (size - len(data)))
        return data[:size]

    def read(self):
        main = self.primary.read() if self.primary else b""
        if not main:
            return b""
        if not self.overlay:
            return main

        ov = self.overlay.read()
        if not ov:
            try:
                self.overlay.cleanup()
            except Exception:
                pass
            self.overlay = None
            return main

        size = max(len(main), len(ov))
        main_p = self._pad(main, size)
        ov_p = self._pad(ov, size)
        try:
            ducked = audioop.mul(main_p, 2, self.duck)
            boosted = audioop.mul(ov_p, 2, self.overlay_gain)
            mixed = audioop.add(ducked, boosted, 2)
            return mixed
        except Exception:
            return main

def get_state(gid):
    if gid not in player_state:
        player_state[gid] = {
            "vc":None,"volume":0.5,"current":None,"queue":[],"loop":False,
            "started_at":None,"elapsed_at_pause":0,"paused":False,
            "history":[],"shuffle":False,"seek_offset":0,"sleep_until":None,"last_current":None,"seeking":False
        }
    return player_state[gid]

async def play_next(gid, seek_to=0):
    s = get_state(gid); vc = s["vc"]
    if not vc or not vc.is_connected(): return
    if s.get("sleep_until") and time.time() >= s["sleep_until"]:
        s["queue"] = []
        s["current"] = None
        s["sleep_until"] = None
        return
    if s["loop"] and s["current"]:
        track = s["current"]
    elif s["queue"]:
        if s["current"]: s["history"].append(s["current"])
        if len(s["history"]) > 50: s["history"] = s["history"][-50:]
        track = s["queue"].pop(0)
        s["current"] = track
        s["last_current"] = track
    else:
        s["current"] = None; s["started_at"] = None; s["seek_offset"] = 0; return
    if not seek_to and isinstance(track, dict) and track.get("_resume_from"):
        seek_to = int(track.get("_resume_from", 0))
    s["seek_offset"] = seek_to
    s["started_at"] = time.time() - seek_to
    s["elapsed_at_pause"] = 0
    s["paused"] = False
    print(f"[*] Playing: {track.get('title')} (Source: {track.get('source')})")

    def after(err):
        if err: print(f"Error: {err}")
        if s.get("seeking"):
            s["seeking"] = False
            return
        asyncio.run_coroutine_threadsafe(play_next(gid), bot.loop)

    try:
        ss_opt = f"-ss {seek_to}" if seek_to > 0 else ""
        if track.get("type") == "file":
            before = ss_opt if ss_opt else None
            src = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(track["file_path"], before_options=before),
                volume=s["volume"]
            )
        else:
            bo = FFMPEG_OPTS["before_options"]
            if ss_opt: bo = ss_opt + " " + bo
            src = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(track["stream_url"], before_options=bo, options=FFMPEG_OPTS["options"]),
                volume=s["volume"]
            )
        vc.play(src, after=after)
    except Exception as e: print(f"play_next: {e}")

@bot.event
async def on_ready():
    print(f"[+] Bot: {bot.user}")
    try: synced = await bot.tree.sync(); print(f"[+] {len(synced)} commands synced")
    except Exception as e: print(f"Sync error: {e}")

@bot.tree.command(name="plus", description="Bot joins your voice channel")
async def plus_cmd(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message("You are not in a voice channel.", ephemeral=True); return
    ch = interaction.user.voice.channel
    s = get_state(interaction.guild_id)
    if s["vc"] and s["vc"].is_connected():
        await s["vc"].move_to(ch); await interaction.response.send_message(f"Moved to **{ch.name}**")
    else:
        s["vc"] = await ch.connect()
        await interaction.response.send_message(f"Joined **{ch.name}** — https://discord.recno.ru")

@bot.tree.command(name="minus", description="Bot leaves the voice channel")
async def minus_cmd(interaction: discord.Interaction):
    s = get_state(interaction.guild_id)
    if s["vc"] and s["vc"].is_connected():
        await s["vc"].disconnect(); s["vc"]=None; s["current"]=None; s["queue"]=[]
        await interaction.response.send_message("Left.")
    else: await interaction.response.send_message("Not in a voice channel.", ephemeral=True)

# ── FLASK ─────────────────────────────────────────────────────
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
CORS(app)

@app.errorhandler(413)
def request_entity_too_large(error):
    print("[!] 413 Error: Request entity too large (reached Flask)")
    return jsonify({"error": "Файл слишком большой (лимит сервера 100МБ)"}), 413

@app.route("/api/config", methods=["GET","POST"])
def api_config():
    cfg = load_config()
    if request.method == "POST":
        data = request.json or {}
        if "guild_id" in data: cfg["guild_id"] = str(data["guild_id"])
        save_config(cfg)
    return jsonify(cfg)

@app.route("/api/search")
def api_search():
    q = request.args.get("q","").strip()
    offset = max(0, int(request.args.get("offset", 0) or 0))
    limit = max(1, min(25, int(request.args.get("limit", 15) or 15)))
    if not q:
        return jsonify({"items": [], "next_offset": 0, "has_more": False})

    def tok(txt):
        txt = (txt or "").lower()
        for ch in [',', '.', '!', '?', '(', ')', '[', ']', '{', '}', '\\', '/', '|', "'", '"']:
            txt = txt.replace(ch, " ")
        return [x for x in txt.split() if x]

    q_tokens = tok(q)
    results = []
    if offset == 0:
        for t in load_library():
            title = (t.get("title") or "").lower()
            artist = (t.get("artist") or "").lower()
            if q.lower() in title or q.lower() in artist:
                results.append({**t, "source": "library"})
                continue
            if q_tokens and any(token in title or token in artist for token in q_tokens):
                results.append({**t, "source": "library"})

    try:
        needed = min(100, offset + limit)
        with yt_dlp.YoutubeDL({"quiet":True,"no_warnings":True,"extract_flat":True,"skip_download":True}) as ydl:
            info = ydl.extract_info(f"ytsearch{needed}:{q}", download=False)
            entries = info.get("entries") or []
            page_entries = entries[offset:offset + limit]
            for e in page_entries:
                vid = e.get("id", "")
                results.append({
                    "id": vid,
                    "title": e.get("title", "Unknown"),
                    "duration": e.get("duration", 0),
                    "thumbnail": f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "source": "youtube"
                })
            has_more = len(entries) > (offset + limit) and needed < 100
    except Exception as e:
        print(f"YT: {e}")
        has_more = False

    return jsonify({
        "items": results,
        "next_offset": offset + limit,
        "has_more": has_more,
    })

@app.route("/api/play", methods=["POST"])
def api_play():
    data = request.json or {}; gid = int(data.get("guild_id",0)); track = data.get("track",{}); play_now = bool(data.get("play_now", False))
    if not gid: return jsonify({"error":"No guild_id"}),400
    s = get_state(gid)
    if not s["vc"] or not s["vc"].is_connected(): return jsonify({"error":"Сначала используй /plus в Discord"}),400
    track_obj = None
    if track.get("source")=="library" and track.get("file_path"):
        track_obj = {"type":"file","title":track.get("title",""),"thumbnail":track.get("thumbnail",""),
            "duration":track.get("duration",0),"file_path":track["file_path"],"artist":track.get("artist","")}
    else:
        try:
            ydl_opts = {**YDL_STREAM}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(track.get("url",""), download=False)
                fmts = info.get("formats",[])
                au = None
                # Ищем лучший аудио-формат
                for f in reversed(fmts):
                    if f.get("acodec") and f["acodec"] != "none" and (f.get("vcodec") == "none" or not f.get("vcodec")):
                        au = f.get("url")
                        break
                if not au and fmts:
                    au = fmts[-1].get("url")
                if not au:
                    au = info.get("url","")
                track_obj = {"type":"stream","title":info.get("title",""),"thumbnail":info.get("thumbnail",""),
                    "duration":info.get("duration",0),"stream_url":au,"original_url":track.get("url",""),"artist":info.get("uploader","")}
        except Exception as e:
            print(f"[!] YT play error: {e}")
            return jsonify({"error":f"Ошибка загрузки: {str(e)}"}),500
    if track_obj:
        if play_now and (s["vc"].is_playing() or s["vc"].is_paused()):
            if s.get("current"):
                s["history"].append(s["current"])
            s["queue"].insert(0, track_obj)
            s["current"] = None
            s["vc"].stop()
        else:
            s["queue"].append(track_obj)
    if not s["vc"].is_playing() and not s["vc"].is_paused():
        asyncio.run_coroutine_threadsafe(play_next(gid), bot.loop)
    return jsonify({"status":"ok"})

@app.route("/api/control", methods=["POST"])
def api_control():
    data = request.json; gid = int(data.get("guild_id",0)); action = data.get("action","")
    s = get_state(gid); vc = s["vc"]
    if action=="pause":
        if vc and vc.is_playing():
            vc.pause()
            s["paused"] = True
            s["elapsed_at_pause"] = int(time.time() - (s["started_at"] or time.time()))
    elif action=="resume":
        if vc and vc.is_paused():
            vc.resume()
            s["paused"] = False
            s["started_at"] = time.time() - s["elapsed_at_pause"]
    elif action=="skip":
        print(f"[*] Control: SKIP (Guild {gid})")
        if vc and (vc.is_playing() or vc.is_paused()): vc.stop()
        else: # Force play next if something is stuck
            asyncio.run_coroutine_threadsafe(play_next(gid), bot.loop)
    elif action=="prev":
        print(f"[*] Control: PREV (Guild {gid})")
        if s.get("history") and len(s["history"]) > 0:
            if s["current"]:
                s["queue"].insert(0, s["current"])
            s["queue"].insert(0, s["history"].pop())
            s["current"] = None
            s["seek_offset"] = 0
            if vc and (vc.is_playing() or vc.is_paused()): vc.stop()
            else: # Force play next if something is stuck
                asyncio.run_coroutine_threadsafe(play_next(gid), bot.loop)
        else:
            print("[!] Control: PREV - History is empty")
    elif action=="stop":
        s["queue"]=[]; s["current"]=None; s["started_at"]=None; s["history"]=[]
        if vc and (vc.is_playing() or vc.is_paused()): vc.stop()
    elif action=="volume":
        try:
            new_vol = float(data.get("value",0.5))
        except (TypeError, ValueError):
            new_vol = 0.5
        s["volume"]=max(0.0,min(2.0,new_vol))
        if vc and vc.source: vc.source.volume=s["volume"]
    elif action=="loop":
        s["loop"]=not s["loop"]
    elif action=="shuffle":
        s["shuffle"]=not s.get("shuffle",False)
        if s["shuffle"] and s["queue"]:
            random.shuffle(s["queue"])
    elif action=="clear_queue":
        s["queue"] = []
    elif action=="sleep_timer":
        mins = int(data.get("minutes", 0) or 0)
        s["sleep_until"] = (time.time() + mins * 60) if mins > 0 else None
    elif action=="disconnect":
        if vc and vc.is_connected():
            asyncio.run_coroutine_threadsafe(vc.disconnect(), bot.loop)
            s["vc"]=None; s["current"]=None; s["queue"]=[]; s["started_at"]=None; s["history"]=[]; s["sleep_until"]=None
    return jsonify({"status":"ok","loop":s.get("loop",False),"shuffle":s.get("shuffle",False),"sleep_until":s.get("sleep_until")})

@app.route("/api/status")
def api_status():
    gid = int(request.args.get("guild_id",0)); s = get_state(gid); vc = s["vc"]
    playing = bool(vc and vc.is_playing())
    paused  = bool(vc and vc.is_paused())
    if playing and s.get("started_at"):
        elapsed = int(time.time() - s["started_at"])
    elif paused:
        elapsed = s.get("elapsed_at_pause", 0)
    else:
        elapsed = 0
    sleep_remaining = 0
    if s.get("sleep_until"):
        sleep_remaining = max(0, int(s["sleep_until"] - time.time()))
    current_for_ui = s["current"] or (s.get("last_current") if (playing or paused) else None)
    return jsonify({
        "connected": bool(vc and vc.is_connected()),
        "playing": playing,
        "paused": paused,
        "volume": s["volume"],
        "current": current_for_ui,
        "queue": s["queue"],
        "loop": s["loop"],
        "shuffle": s.get("shuffle",False),
        "has_prev": len(s.get("history",[])) > 0,
        "elapsed": elapsed,
        "sleep_remaining": sleep_remaining,
        "history": list(reversed((s.get("history") or [])[-30:])),
    })


@app.route("/api/tts", methods=["POST"])
def api_tts():
    data = request.json or {}
    gid = int(data.get("guild_id", 0))
    text = (data.get("text") or "").strip()
    lang = (data.get("lang") or "ru").strip() or "ru"
    if not gid:
        return jsonify({"error": "No guild_id"}), 400
    if not text:
        return jsonify({"error": "Введите текст"}), 400
    if len(text) > 300:
        return jsonify({"error": "Максимум 300 символов"}), 400

    s = get_state(gid)
    vc = s["vc"]
    if not vc or not vc.is_connected():
        return jsonify({"error": "Сначала используй /plus в Discord"}), 400

    try:
        tts_id = str(uuid.uuid4())
        fp = os.path.join(UPLOAD_FOLDER, f"tts_{tts_id}.mp3")
        gTTS(text=text, lang=lang).save(fp)

        # If something is currently playing, overlay TTS live without restarting track.
        if vc.is_playing() and vc.source:
            overlay_src = discord.FFmpegPCMAudio(fp, options='-vn')
            cur_src = vc.source
            if isinstance(cur_src, OverlayAudioSource):
                cur_src.add_overlay(overlay_src)
            else:
                vc.source = OverlayAudioSource(cur_src, overlay_src)
            return jsonify({"status": "ok", "mode": "overlay"})

        # If paused or idle, queue as regular TTS item.
        tts_dur = 2
        try:
            tts_a = MutaFile(fp)
            if tts_a and tts_a.info:
                tts_dur = max(1, int(tts_a.info.length))
        except Exception:
            tts_dur = max(1, min(20, len(text) // 14 + 1))

        tts_track = {
            "id": tts_id,
            "type": "file",
            "source": "tts",
            "title": "TTS сообщение",
            "artist": "Озвучка",
            "duration": tts_dur,
            "thumbnail": "",
            "file_path": fp,
        }
        s["queue"].insert(0, tts_track)
        if not vc.is_paused() and not vc.is_playing():
            asyncio.run_coroutine_threadsafe(play_next(gid), bot.loop)
        return jsonify({"status": "ok", "mode": "queued"})
    except Exception as e:
        return jsonify({"error": f"Не удалось озвучить: {e}"}), 500

@app.route("/api/seek", methods=["POST"])
def api_seek():
    data = request.json; gid = int(data.get("guild_id",0)); pos = int(data.get("position",0))
    s = get_state(gid); vc = s["vc"]
    if not vc or not s["current"]: return jsonify({"error":"Ничего не играет"}),400
    if pos < 0: pos = 0
    dur = s["current"].get("duration",0)
    if dur and pos > dur: pos = dur
    # Останавливаем текущее и перезапускаем с нужной позиции
    s["seeking"] = True
    if vc.is_playing() or vc.is_paused():
        vc.stop()
    # Небольшая задержка чтобы stop() сработал
    import time as _t; _t.sleep(0.1)
    asyncio.run_coroutine_threadsafe(_seek_play(gid, pos), bot.loop)
    return jsonify({"status":"ok","position":pos})

async def _seek_play(gid, pos):
    s = get_state(gid); vc = s["vc"]
    if not vc or not vc.is_connected() or not s["current"]:
        s["seeking"] = False
        return
    track = s["current"]
    s["seek_offset"] = pos
    s["started_at"] = time.time() - pos
    s["elapsed_at_pause"] = 0
    s["paused"] = False
    def after(err):
        if err: print(f"Seek error: {err}")
        asyncio.run_coroutine_threadsafe(play_next(gid), bot.loop)
    try:
        ss_opt = f"-ss {pos}"
        if track.get("type") == "file":
            src = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(track["file_path"], before_options=ss_opt),
                volume=s["volume"]
            )
        else:
            bo = ss_opt + " " + FFMPEG_OPTS["before_options"]
            src = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(track["stream_url"], before_options=bo, options=FFMPEG_OPTS["options"]),
                volume=s["volume"]
            )
        vc.play(src, after=after)
    except Exception as e:
        s["seeking"] = False
        print(f"seek_play: {e}")

@app.route("/api/queue/remove", methods=["POST"])
def api_queue_remove():
    data = request.json; gid = int(data.get("guild_id",0)); idx = int(data.get("index",0))
    s = get_state(gid)
    if 0 <= idx < len(s["queue"]):
        removed = s["queue"].pop(idx)
        return jsonify({"status":"ok","removed":removed.get("title","")})
    return jsonify({"error":"Invalid index"}),400

@app.route("/api/queue/move", methods=["POST"])
def api_queue_move():
    data = request.json; gid = int(data.get("guild_id",0))
    frm = int(data.get("from",0)); to = int(data.get("to",0))
    s = get_state(gid)
    q = s["queue"]
    if 0 <= frm < len(q) and 0 <= to < len(q) and frm != to:
        item = q.pop(frm)
        q.insert(to, item)
        return jsonify({"status":"ok"})
    return jsonify({"error":"Invalid index"}),400

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        print("[!] Upload error: No file in request")
        return jsonify({"error":"No file"}),400
    f = request.files["file"]; title=request.form.get("title",f.filename); artist=request.form.get("artist","Unknown")
    tid=str(uuid.uuid4()); ext=os.path.splitext(f.filename)[1].lower() or ".mp3"
    fp=os.path.join(UPLOAD_FOLDER,tid+ext)
    try:
        f.save(fp)
        print(f"[*] File saved: {fp} ({title})")
    except Exception as e:
        print(f"[!] File save error: {e}")
        return jsonify({"error":f"Could not save file: {str(e)}"}),500
    dur=0
    try:
        a=MutaFile(fp)
        if a and a.info: dur=int(a.info.length)
    except: pass
    lib=load_library()
    lib.append({"id":tid,"title":title,"artist":artist,"duration":dur,"thumbnail":"",
        "file_path":fp,"source":"library","type":"file"})
    save_library(lib)
    print(f"[+] Library updated with {tid}")
    return jsonify({"status":"ok","track_id":tid})

@app.route("/api/library")
def api_library(): return jsonify(load_library())

@app.route("/api/library/<tid>", methods=["PUT"])
def api_update(tid):
    data=request.json; lib=load_library()
    for t in lib:
        if t["id"]==tid:
            for k in ["title","artist","thumbnail"]:
                if k in data: t[k]=data[k]
            break
    save_library(lib); return jsonify({"status":"ok"})

@app.route("/api/library/<tid>", methods=["DELETE"])
def api_delete(tid):
    lib=load_library()
    for t in lib:
        if t["id"]==tid and t.get("file_path"):
            try: os.remove(t["file_path"])
            except: pass
    save_library([t for t in lib if t["id"]!=tid])
    return jsonify({"status":"ok"})

@app.route("/uploads/<path:fn>")
def serve_upload(fn):
    from flask import send_from_directory
    return send_from_directory(UPLOAD_FOLDER,fn)

HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no,viewport-fit=cover"/>
<meta name="mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
<meta name="theme-color" content="#08080f"/>
<title>MusicBot</title>
<style>
:root{
  --bg:#08080f;
  --s1:rgba(255,255,255,0.04);
  --s2:rgba(255,255,255,0.07);
  --border:rgba(255,255,255,0.07);
  --accent:#7c3aed;
  --accent2:#a78bfa;
  --glow:rgba(124,58,237,0.4);
  --green:#34d399;
  --red:#f87171;
  --text:#f0efff;
  --sub:#64748b;
  --sub2:#94a3b8;
  --r:16px;
  --r2:12px;
  --nav-h:64px;
  --safe-b:env(safe-area-inset-bottom,0px);
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;-webkit-font-smoothing:antialiased}
html,body{height:100%;overflow:hidden}
body{
  height:100%;
  background:var(--bg);
  color:var(--text);
  font-family:-apple-system,'SF Pro Display','Inter',system-ui,sans-serif;
  display:flex;flex-direction:column;
  background-image:
    radial-gradient(ellipse 100% 60% at 10% 0%,rgba(124,58,237,0.12) 0%,transparent 55%),
    radial-gradient(ellipse 70% 50% at 90% 100%,rgba(99,102,241,0.08) 0%,transparent 55%);
}
::-webkit-scrollbar{display:none}

@keyframes fadeUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes scaleIn{from{opacity:0;transform:scale(.94)}to{opacity:1;transform:scale(1)}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes glow-p{0%{box-shadow:0 0 18px rgba(124,58,237,.28);transform:scale(1)}50%{box-shadow:0 0 34px rgba(124,58,237,.45);transform:scale(1.02)}100%{box-shadow:0 0 18px rgba(124,58,237,.28);transform:scale(1)}}
@keyframes eq1{0%,100%{transform:scaleY(.25)}50%{transform:scaleY(1)}}
@keyframes eq2{0%,100%{transform:scaleY(.6)}33%{transform:scaleY(.15)}66%{transform:scaleY(1)}}
@keyframes eq3{0%,100%{transform:scaleY(.8)}40%{transform:scaleY(.1)}80%{transform:scaleY(.9)}}
@keyframes slide-up{from{transform:translateY(100%);opacity:0}to{transform:translateY(0);opacity:1}}
@keyframes pulse-s{0%,100%{opacity:1}50%{opacity:.4}}

/* ── SCREENS */
.screen{display:none;flex-direction:column;flex:1;overflow:hidden;animation:fadeIn .2s ease}
.screen.active{display:flex}

/* ── TOP BAR */
.topbar{
  display:flex;align-items:center;gap:10px;
  padding:14px 20px 10px;
  padding-top:calc(14px + env(safe-area-inset-top,0px));
  flex-shrink:0;
  background:rgba(8,8,15,0.7);
  backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);
}
.topbar-title{font-size:1.2rem;font-weight:700;letter-spacing:-.4px;flex:1}

/* ── SCROLL AREA */
.scroll{overflow-y:auto;flex:1;-webkit-overflow-scrolling:touch}

/* ── BOTTOM CONTROLS (mini-player + nav stacked) */
.bottom-stack{flex-shrink:0;display:flex;flex-direction:column}

/* ── MINI PLAYER */
.mini-player{
  margin:0 12px 6px;
  background:rgba(22,18,36,0.92);
  backdrop-filter:blur(24px);
  -webkit-backdrop-filter:blur(24px);
  border:1px solid rgba(124,58,237,0.25);
  border-radius:14px;
  padding:9px 12px;
  display:none;
  align-items:center;
  gap:10px;
  cursor:pointer;
  transition:transform .15s,opacity .2s;
  position:relative;
  overflow:hidden;
}
.mini-player.visible{display:flex;animation:fadeUp .25s ease}
.mini-player:active{transform:scale(.98)}
.mini-art{width:36px;height:36px;border-radius:8px;object-fit:cover;flex-shrink:0;background:rgba(124,58,237,0.2)}
.mini-info{flex:1;min-width:0}
.mini-title{font-size:.8rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mini-sub{font-size:.66rem;color:var(--sub);margin-top:1px}
.mini-btns{display:flex;gap:2px;align-items:center;flex-shrink:0}
.mini-btn{
  width:34px;height:34px;border-radius:50%;background:none;border:none;
  color:var(--sub2);cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:color .15s,background .15s;
}
.mini-btn:active{color:#fff;background:rgba(255,255,255,0.08)}
.mini-btn svg{width:15px;height:15px;pointer-events:none}
.mini-prog{
  position:absolute;bottom:0;left:0;right:0;height:2px;
  background:rgba(255,255,255,0.06);
}
.mini-prog-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .5s linear;border-radius:0 0 14px 14px}

/* ── BOTTOM NAV */
.nav{
  display:flex;
  background:rgba(8,8,15,0.95);
  backdrop-filter:blur(32px);
  -webkit-backdrop-filter:blur(32px);
  border-top:1px solid var(--border);
  padding-bottom:var(--safe-b);
  flex-shrink:0;
}
.nav-btn{
  flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:3px;padding:10px 4px 6px;
  background:none;border:none;cursor:pointer;
  color:var(--sub);font-size:.58rem;font-weight:600;
  font-family:inherit;letter-spacing:.3px;text-transform:uppercase;
  transition:color .2s;position:relative;
}
.nav-btn svg{width:21px;height:21px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;transition:all .2s}
.nav-btn.active{color:var(--accent2)}
.nav-btn.active svg{stroke:var(--accent2)}
.nav-btn::after{
  content:'';position:absolute;top:0;left:50%;transform:translateX(-50%);
  width:0;height:2px;background:var(--accent2);border-radius:0 0 3px 3px;
  transition:width .25s cubic-bezier(.34,1.56,.64,1);
}
.nav-btn.active::after{width:24px}

/* ─────── HOME ─────── */
.home-hero{padding:8px 20px 20px}
.hello{font-size:.9rem;color:var(--sub2);margin-bottom:10px}
.server-chip{
  display:inline-flex;align-items:center;gap:7px;
  background:var(--s1);border:1px solid var(--border);
  border-radius:99px;padding:6px 14px 6px 10px;
  font-size:.74rem;color:var(--sub2);cursor:pointer;
  transition:background .2s;
}
.server-chip:active{background:var(--s2)}
.chip-dot{width:7px;height:7px;border-radius:50%;background:var(--sub);transition:all .3s;flex-shrink:0}
.chip-dot.on{background:var(--green);box-shadow:0 0 6px var(--green)}
.chip-dot.playing{background:var(--accent2);box-shadow:0 0 6px var(--accent2);animation:pulse-s 1.5s ease-in-out infinite}

.sec-title{font-size:.62rem;font-weight:700;letter-spacing:.7px;color:var(--sub);text-transform:uppercase;padding:0 20px 10px}
.lib-grid{padding:0 12px 16px;display:grid;grid-template-columns:1fr 1fr;gap:10px}
.lib-card{
  background:var(--s1);border:1px solid var(--border);
  border-radius:var(--r);overflow:hidden;cursor:pointer;
  transition:transform .15s,background .15s;
  animation:fadeUp .25s ease backwards;
}
.lib-card:active{transform:scale(.96);background:var(--s2)}
.lib-card-art{width:100%;aspect-ratio:1;object-fit:cover;background:rgba(124,58,237,0.12);display:block}
.lib-card-body{padding:10px}
.lib-card-title{font-size:.8rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lib-card-sub{font-size:.66rem;color:var(--sub);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ─────── SEARCH ─────── */
.search-header{
  padding:10px 16px 12px;
  background:rgba(8,8,15,0.85);
  backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);
  flex-shrink:0;
}
.search-box{
  display:flex;align-items:center;gap:10px;
  background:var(--s1);border:1px solid var(--border);
  border-radius:var(--r2);padding:11px 14px;
  transition:border-color .2s,background .2s;
}
.search-box:focus-within{border-color:rgba(124,58,237,.45);background:rgba(124,58,237,0.05)}
.search-box svg{width:16px;height:16px;stroke:var(--sub);flex-shrink:0;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;fill:none;transition:stroke .2s}
.search-box:focus-within svg{stroke:var(--accent2)}
.s-input{flex:1;background:none;border:none;outline:none;color:var(--text);font-size:.9rem;font-family:inherit}
.s-input::placeholder{color:var(--sub)}
.s-clear{background:none;border:none;color:var(--sub);cursor:pointer;padding:2px;display:none}
.s-clear.show{display:flex}
.s-clear svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}

.result-item{
  display:flex;align-items:center;gap:12px;
  padding:10px 16px;cursor:pointer;
  transition:background .12s;
  animation:fadeUp .2s ease backwards;
}
.result-item:active{background:rgba(255,255,255,0.04)}
.r-thumb{width:50px;height:50px;border-radius:10px;object-fit:cover;flex-shrink:0;background:rgba(124,58,237,0.12)}
.r-info{flex:1;min-width:0}
.r-title{font-size:.85rem;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.r-sub{font-size:.68rem;color:var(--sub);margin-top:3px;display:flex;align-items:center;gap:6px}
.tag{font-size:.58rem;font-weight:700;letter-spacing:.3px;padding:2px 7px;border-radius:99px}
.tag-lib{background:rgba(52,211,153,.1);color:var(--green);border:1px solid rgba(52,211,153,.18)}
.tag-yt{background:rgba(248,113,113,.1);color:var(--red);border:1px solid rgba(248,113,113,.18)}
.r-play{
  width:36px;height:36px;border-radius:50%;
  background:var(--s1);border:1px solid var(--border);
  color:var(--sub2);cursor:pointer;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  transition:all .15s;
}
.r-play svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:2.5;stroke-linecap:round;pointer-events:none}
.r-play:active{background:var(--accent);border-color:var(--accent);color:#fff;transform:scale(.9)}

.sec-head{font-size:.62rem;font-weight:700;letter-spacing:.6px;color:var(--sub);text-transform:uppercase;padding:12px 16px 6px}

/* ─────── PLAYER ─────── */
.player-wrap{
  flex:1;display:flex;flex-direction:column;
  padding:0 22px 18px;
  overflow-y:auto;
  -webkit-overflow-scrolling:touch;
}
.art-wrap{
  flex:1;display:flex;align-items:center;justify-content:center;
  padding:16px 0 14px;min-height:0;
}
.p-art{
  width:min(72vw,280px);height:min(72vw,280px);
  border-radius:22px;object-fit:cover;
  background:rgba(124,58,237,0.15);
  box-shadow:0 20px 60px rgba(0,0,0,.5);
  transition:box-shadow .5s;display:block;
}
.p-art.lit{animation:glow-p 6s cubic-bezier(.4,0,.2,1) infinite;will-change:transform,box-shadow}

.p-meta{margin-bottom:18px}
.p-title{font-size:1.2rem;font-weight:700;letter-spacing:-.3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.p-artist{font-size:.82rem;color:var(--sub2);margin-top:3px}

/* Progress */
.prog-wrap{margin-bottom:16px}
.prog-bar{
  width:100%;height:4px;
  background:rgba(255,255,255,0.08);
  border-radius:99px;cursor:pointer;
  position:relative;touch-action:none;
  transition:height .15s;
  padding:14px 0;
  background-clip:content-box;
  -webkit-background-clip:content-box;
}
.prog-bar:active{height:6px}
.prog-filled{
  height:4px;
  position:absolute;
  top:14px;left:0;
  background:linear-gradient(90deg,var(--accent),var(--accent2));
  border-radius:99px;pointer-events:none;
  transition:width .5s linear;
}
.prog-filled::after{
  content:'';position:absolute;right:-9px;top:50%;transform:translateY(-50%);
  width:18px;height:18px;background:#fff;border-radius:50%;
  box-shadow:0 0 8px rgba(124,58,237,.6);
  opacity:0;transition:opacity .2s;
}
.prog-bar:active .prog-filled::after{opacity:1}
.prog-times{display:flex;justify-content:space-between;margin-top:2px}
.p-time{font-size:.72rem;color:var(--sub);font-variant-numeric:tabular-nums}

/* Controls */
.ctrl-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
.c-side{
  width:44px;height:44px;border-radius:50%;background:none;border:none;
  color:var(--sub2);cursor:pointer;
  display:flex;align-items:center;justify-content:center;transition:color .2s;
}
.c-side svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}
.c-side:active{opacity:.5}
.c-side.on{color:var(--accent2)}
.c-skip{
  width:52px;height:52px;border-radius:50%;
  background:var(--s1);border:1px solid var(--border);
  color:var(--text);cursor:pointer;
  display:flex;align-items:center;justify-content:center;transition:all .15s;
}
.c-skip svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}
.c-skip:active{background:var(--s2);transform:scale(.92)}

/* PLAY BUTTON — fixed layout, no disappearing */
.c-play{
  width:64px;height:64px;border-radius:50%;
  background:linear-gradient(135deg,#7c3aed,#6d28d9);
  border:none;color:#fff;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 6px 24px rgba(124,58,237,.5);
  transition:transform .15s,box-shadow .2s;
  flex-shrink:0;
  position:relative;overflow:hidden;
}
.c-play:active{transform:scale(.92);box-shadow:0 3px 12px rgba(124,58,237,.4)}
/* icon wrapper — always 26x26, content switches */
.c-play-ico{
  width:26px;height:26px;
  display:flex;align-items:center;justify-content:center;
  pointer-events:none;
  position:relative;
}
.c-play-ico svg{
  position:absolute;top:0;left:0;width:26px;height:26px;
  fill:#fff;stroke:none;
  transition:opacity .15s;
}
.c-play-ico .ico-play{opacity:1}
.c-play-ico .ico-pause{opacity:0}
.c-play.playing .c-play-ico .ico-play{opacity:0}
.c-play.playing .c-play-ico .ico-pause{opacity:1}

/* Volume */
.vol-row{display:flex;align-items:center;gap:10px}

.sleep-row{display:flex;gap:8px;align-items:center;margin-top:10px;justify-content:center;flex-wrap:wrap}
.sleep-btn{height:32px;padding:0 10px;border-radius:10px}
.v-icon{color:var(--sub);flex-shrink:0;cursor:pointer;display:flex;align-items:center}
.v-icon svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
input[type=range]{
  flex:1;height:4px;appearance:none;
  background:rgba(255,255,255,0.08);
  border-radius:99px;outline:none;cursor:pointer;
}
input[type=range]::-webkit-slider-thumb{
  appearance:none;width:24px;height:24px;
  background:#fff;border-radius:50%;cursor:pointer;
  box-shadow:0 2px 8px rgba(0,0,0,.3);
}
input[type=range]::-moz-range-thumb{
  width:24px;height:24px;
  background:#fff;border-radius:50%;cursor:pointer;
  box-shadow:0 2px 8px rgba(0,0,0,.3);border:none;
}
.v-val{font-size:.7rem;color:var(--sub);min-width:34px;text-align:right}


/* ─────── PROFILE ─────── */
.profile-body{padding:0 16px 16px;display:flex;flex-direction:column;gap:14px}
.p-card{background:var(--s1);border:1px solid var(--border);border-radius:var(--r);padding:16px}
.p-card-label{font-size:.62rem;font-weight:700;letter-spacing:.5px;color:var(--sub);text-transform:uppercase;margin-bottom:12px}
.input-row{display:flex;gap:8px}
.field{
  flex:1;background:rgba(255,255,255,0.04);
  border:1px solid var(--border);color:var(--text);
  padding:10px 14px;border-radius:var(--r2);
  font-size:.84rem;font-family:inherit;outline:none;
  transition:border-color .2s,background .2s;
}
.field:focus{border-color:rgba(124,58,237,.4);background:rgba(124,58,237,0.05)}
.field::placeholder{color:var(--sub)}
.status-row{display:flex;align-items:center;gap:8px;margin-top:10px}
.s-dot{width:7px;height:7px;border-radius:50%;background:var(--sub);transition:all .3s;flex-shrink:0}
.s-dot.on{background:var(--green);box-shadow:0 0 6px var(--green)}
.s-dot.pl{background:var(--accent2);box-shadow:0 0 6px var(--accent2);animation:pulse-s 1.5s ease-in-out infinite}
.s-txt{font-size:.74rem;color:var(--sub)}

.save-btn{
  background:linear-gradient(135deg,var(--accent),#6d28d9);
  border:none;color:#fff;padding:10px 18px;
  border-radius:var(--r2);font-size:.82rem;font-weight:600;
  font-family:inherit;cursor:pointer;white-space:nowrap;transition:opacity .2s;
}
.save-btn:active{opacity:.7}

.dis-btn{
  width:100%;
  background:rgba(248,113,113,0.07);
  border:1px solid rgba(248,113,113,0.2);
  color:var(--red);padding:13px;border-radius:var(--r2);
  font-size:.84rem;font-weight:600;font-family:inherit;cursor:pointer;
  display:flex;align-items:center;justify-content:center;gap:8px;
  transition:background .2s;
}
.dis-btn:active{background:rgba(248,113,113,0.14)}
.dis-btn svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

.upload-zone{
  background:rgba(124,58,237,0.04);
  border:1.5px dashed rgba(124,58,237,.25);
  border-radius:var(--r);padding:20px;cursor:pointer;
  display:flex;flex-direction:column;align-items:center;gap:10px;
  transition:all .2s;
}
.upload-zone:active{background:rgba(124,58,237,.08);border-color:rgba(124,58,237,.45)}
.upload-zone svg{width:28px;height:28px;stroke:var(--accent2);fill:none;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.upload-zone-text strong{display:block;font-size:.84rem;color:var(--text);margin-bottom:3px;text-align:center}
.upload-zone-text span{font-size:.72rem;color:var(--sub);text-align:center}

.up-fields{display:none;flex-direction:column;gap:10px;animation:fadeUp .2s ease}
.up-fields.show{display:flex}
.up-fields .field{background:rgba(255,255,255,0.04)}
.up-btns{display:flex;gap:8px}
.up-btns .save-btn{flex:1;padding:11px}
.cancel-btn{
  background:var(--s1);border:1px solid var(--border);
  color:var(--sub2);padding:11px 16px;border-radius:var(--r2);
  font-size:.82rem;font-weight:600;font-family:inherit;cursor:pointer;transition:background .15s;
}
.cancel-btn:active{background:var(--s2)}

.lib-rows{display:flex;flex-direction:column;gap:2px}
.lib-row{
  display:flex;align-items:center;gap:12px;
  padding:10px;border-radius:var(--r2);cursor:pointer;
  transition:background .12s;
  animation:fadeUp .2s ease backwards;
}
.lib-row:active{background:var(--s2)}
.lr-art{width:46px;height:46px;border-radius:10px;object-fit:cover;flex-shrink:0;background:rgba(124,58,237,.12)}
.lr-info{flex:1;min-width:0;display:flex;flex-direction:column;justify-content:center}
.lr-title{font-size:.85rem;font-weight:600;color:var(--text);margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lr-sub{font-size:.7rem;color:var(--sub);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ── THUMBNAIL PLACEHOLDER */
.t-placeholder{
  width:100%;height:100%;
  background:linear-gradient(135deg, var(--s1), var(--s2));
  display:flex;align-items:center;justify-content:center;
  color:var(--accent);border-radius:inherit;
}
.t-placeholder svg{width:40%;height:40%;opacity:.7}
.lr-btns{display:flex;gap:4px;flex-shrink:0}
.lr-btn{
  width:30px;height:30px;border-radius:8px;background:none;border:none;
  color:var(--sub);cursor:pointer;
  display:flex;align-items:center;justify-content:center;transition:all .15s;
}
.lr-btn svg{width:13px;height:13px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}
.lr-btn:active{background:var(--s2);color:var(--text)}
.lr-btn.del:active{background:rgba(248,113,113,.12);color:var(--red)}

/* ─────── OVERLAYS ─────── */
.overlay{
  display:none;position:fixed;inset:0;
  background:rgba(0,0,0,.6);
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  z-index:200;align-items:flex-end;justify-content:center;
}
.overlay.show{display:flex;animation:fadeIn .2s ease}
.sheet{
  width:100%;max-width:500px;
  background:rgba(14,12,26,0.98);
  backdrop-filter:blur(32px);-webkit-backdrop-filter:blur(32px);
  border:1px solid var(--border);
  border-radius:22px 22px 0 0;
  padding:18px 20px calc(18px + var(--safe-b));
}
.sheet-anim{animation:slide-up .26s cubic-bezier(.34,1.2,.64,1)}
.handle{width:38px;height:4px;background:rgba(255,255,255,.12);border-radius:99px;margin:0 auto 18px}
.sheet-title{font-size:.95rem;font-weight:700;margin-bottom:16px}
.sheet .field{width:100%;margin-bottom:10px}
.sheet-btns{display:flex;gap:8px;justify-content:flex-end;margin-top:6px}
.sheet-btns .save-btn{padding:10px 22px}
.sheet-btns .cancel-btn{padding:10px 16px}

.q-item{
  display:flex;align-items:center;gap:10px;
  padding:8px 4px;border-radius:10px;transition:background .12s;
  animation:fadeUp .2s ease backwards;
}
.q-item:active{background:var(--s2)}
.q-n{color:var(--accent2);font-weight:700;font-size:.7rem;min-width:18px;text-align:center}
.q-thumb{width:40px;height:40px;border-radius:8px;object-fit:cover;flex-shrink:0;background:rgba(124,58,237,.12)}
.q-info{flex:1;min-width:0}
.q-title{font-size:.8rem;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.q-dur{font-size:.66rem;color:var(--sub);margin-top:1px}

/* TOAST */
.toasts{
  position:fixed;
  top:calc(12px + env(safe-area-inset-top,0px));
  left:50%;transform:translateX(-50%);
  width:calc(100% - 32px);max-width:340px;
  display:flex;flex-direction:column;gap:6px;
  z-index:300;pointer-events:none;
}
.toast{
  background:rgba(16,14,28,.97);
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border:1px solid var(--border);border-radius:12px;
  padding:11px 16px;font-size:.8rem;color:var(--sub2);
  display:flex;align-items:center;gap:10px;
  box-shadow:0 8px 32px rgba(0,0,0,.4);
  animation:fadeUp .25s ease;
}
.toast.ok{border-color:rgba(52,211,153,.22)}
.toast.err{border-color:rgba(248,113,113,.22)}
.t-d{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.toast.ok .t-d{background:var(--green)}
.toast.err .t-d{background:var(--red)}

/* MISC */
.spinner{width:30px;height:30px;border:2px solid rgba(255,255,255,.06);border-top-color:var(--accent2);border-radius:50%;animation:spin .6s linear infinite}
.loader{display:flex;flex-direction:column;align-items:center;padding:48px;gap:14px;color:var(--sub);font-size:.8rem}
.empty{display:flex;flex-direction:column;align-items:center;padding:48px 20px;gap:12px;color:var(--sub);text-align:center;animation:fadeIn .3s ease}
.empty-i{width:50px;height:50px;background:var(--s1);border:1px solid var(--border);border-radius:14px;display:flex;align-items:center;justify-content:center}
.empty-i svg{width:22px;height:22px;stroke:var(--sub);fill:none;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.empty p{font-size:.8rem;line-height:1.6}

/* ─────── LOAD PANEL ─────── */
.load-panel{
  position:fixed;bottom:calc(var(--nav-h) + var(--safe-b) + 10px);left:10px;right:10px;
  background:rgba(14,12,26,0.95);border:1px solid var(--border);
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  border-radius:14px;padding:12px 16px;
  display:flex;align-items:center;gap:12px;
  box-shadow:0 10px 40px rgba(0,0,0,.5);
  transform:translateY(30px);opacity:0;pointer-events:none;visibility:hidden;
  transition:transform .4s cubic-bezier(.19,1,.22,1),opacity .4s,visibility .4s;
  z-index:900;
}
.load-panel.show{transform:translateY(0);opacity:1;pointer-events:all;visibility:visible}
.lp-spinner{width:18px;height:18px;border:2px solid rgba(124,58,237,.3);border-top-color:var(--accent);border-radius:50%;animation:spin .6s linear infinite}
.lp-text{font-size:.8rem;color:var(--text);flex:1}

/* ─────── MOBILE RESPONSIVE ─────── */
@media(max-height:640px){
  .p-art{width:min(55vw,200px);height:min(55vw,200px);border-radius:18px}
  .art-wrap{padding:8px 0 8px}
  .p-meta{margin-bottom:10px}
  .p-title{font-size:1rem}
  .prog-wrap{margin-bottom:10px}
  .ctrl-row{margin-bottom:12px}
  .c-play{width:56px;height:56px}
  .c-skip{width:44px;height:44px}
  .c-side{width:38px;height:38px}
  .sleep-btn{height:28px;padding:0 8px;font-size:.7rem}
  .sleep-row .p-time{width:100%;text-align:center}
  .player-wrap{padding:0 18px 10px}
}
@media(max-width:380px){
  .ctrl-row{gap:2px}
  .c-play{width:54px;height:54px}
  .c-skip{width:42px;height:42px}
  .c-side{width:36px;height:36px}
  .c-side svg{width:18px;height:18px}
  .c-skip svg{width:16px;height:16px}
  .sleep-row{gap:6px;margin-top:8px}
  .sleep-btn{height:26px;padding:0 7px;font-size:.66rem}
  .player-wrap{padding:0 14px 10px}
  .r-thumb{width:44px;height:44px;border-radius:8px}
  .result-item{padding:8px 12px;gap:10px}
  .nav-btn{font-size:.52rem;padding:8px 2px 5px}
  .nav-btn svg{width:18px;height:18px}
}
@media(max-width:340px){
  .lib-grid{grid-template-columns:1fr}
}
</style>

</head>
<body>

<!-- HOME -->
<div class="screen active" id="s-home">
  <div class="topbar"><div class="topbar-title">MusicBot</div></div>
  <div class="scroll">
    <div class="home-hero">
      <div class="hello">Добро пожаловать</div>
      <div class="server-chip" onclick="goScreen('profile')">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
        <span id="chipLabel">Выбрать сервер</span>
        <div class="chip-dot" id="chipDot"></div>
      </div>
    </div>
    <div class="sec-title">Библиотека</div>
    <div class="lib-grid" id="homeGrid"></div>
  </div>
</div>

<!-- SEARCH -->
<div class="screen" id="s-search">
  <div class="search-header">
    <div class="search-box">
      <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input class="s-input" id="searchIn" placeholder="Трек, исполнитель..." oninput="onSInput()" onkeydown="if(event.key==='Enter')doSearch()"/>
      <button class="s-clear" id="sClear" onclick="clearS()">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
  </div>
  <div class="scroll" id="sResults" style="overflow-y:auto">
    <div class="empty"><div class="empty-i"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div><p>Введи название трека<br/>или исполнителя</p></div>
  </div>
</div>

<!-- PLAYER -->
<div class="screen" id="s-player">
  <div class="topbar">
    <div class="topbar-title" style="flex:1">Плеер</div>
    <button onclick="openQueue()" style="background:none;border:none;color:var(--sub2);cursor:pointer;padding:4px;display:flex;align-items:center">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
    </button>
  </div>
  <div class="player-wrap">
    <div class="art-wrap">
      <div id="pArt" style="width:100%;height:100%;display:contents"></div>
    </div>
    <div class="p-meta">
      <div class="p-title" id="pTitle">Ничего не играет</div>
      <div class="p-artist" id="pArtist">—</div>
    </div>
    <div class="prog-wrap">
      <div class="prog-bar" id="progBar"><div class="prog-filled" id="progFill" style="width:0%"></div></div>
      <div class="prog-times"><span class="p-time" id="tNow">0:00</span><span class="p-time" id="tEnd">0:00</span></div>
    </div>
    <div class="ctrl-row">
      <button class="c-side" id="btnShuffle" onclick="toggleShuffle()">
        <svg viewBox="0 0 24 24"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/></svg>
      </button>
      <button class="c-skip" onclick="doPrev()">
        <svg viewBox="0 0 24 24"><polygon points="19 20 9 12 19 4 19 20"/><line x1="5" y1="19" x2="5" y2="5"/></svg>
      </button>
      <button class="c-play" id="btnPlay" onclick="togglePlay()">
        <div class="c-play-ico">
          <svg class="ico-play" viewBox="0 0 24 24"><polygon points="6 3 20 12 6 21 6 3"/></svg>
          <svg class="ico-pause" viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
        </div>
      </button>
      <button class="c-skip" onclick="doSkip()">
        <svg viewBox="0 0 24 24"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19"/></svg>
      </button>
      <button class="c-side" id="btnLoop" onclick="toggleLoop()">
        <svg viewBox="0 0 24 24"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 014-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 01-4 4H3"/></svg>
      </button>
    </div>
    <div class="vol-row">
      <span class="v-icon" onclick="mute()"><svg viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 010 14.14M15.54 8.46a5 5 0 010 7.07"/></svg></span>
      <input type="range" id="volSlider" min="0" max="200" value="50" oninput="setVol(this.value)"/>
      <span class="v-val" id="volVal">50%</span>
    </div>
    <div class="sleep-row">
      <button class="lr-btn sleep-btn" onclick="setSleepTimer(15)">Сон 15м</button>
      <button class="lr-btn sleep-btn" onclick="setSleepTimer(30)">Сон 30м</button>
      <button class="lr-btn sleep-btn" onclick="setSleepTimer(0)">Сон выкл</button>
      <span class="p-time" id="sleepInfo">Сон: выкл</span>
    </div>
    </div>
  </div>
</div>

<!-- PROFILE -->
<div class="screen" id="s-profile">
  <div class="topbar"><div class="topbar-title">Профиль</div></div>
  <div class="scroll">
    <div class="profile-body">
      <div class="p-card">
        <div class="p-card-label">Discord Server</div>
        <div class="input-row">
          <input class="field" id="guildInput" placeholder="Guild ID сервера"/>
          <button class="save-btn" onclick="applyGuild()">OK</button>
        </div>
        <div class="status-row">
          <div class="s-dot" id="pDot"></div>
          <span class="s-txt" id="pStatus">Не подключён</span>
        </div>
      </div>
      <button class="dis-btn" onclick="doDisconnect()">
        <svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/></svg>
        Отключить бота от канала
      </button>
      <div class="p-card" style="margin-top:12px">
        <div class="p-card-label">Озвучивание (TTS)</div>
        <div class="input-row" style="display:flex;gap:8px;align-items:center">
          <input class="field" id="ttsText" maxlength="300" placeholder="Текст для озвучки поверх музыки"/>
          <button class="save-btn" onclick="sendTTS()">Озвучить</button>
        </div>
      </div>
      <label class="upload-zone" for="fileIn">
        <svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        <div class="upload-zone-text"><strong>Загрузить свой трек</strong><span>Нажми чтобы выбрать аудио файл</span></div>
      </label>
      <input type="file" id="fileIn" accept="audio/*" style="display:none" onchange="onFile()"/>
      <div class="up-fields" id="upFields">
        <input class="field" id="upTitle" placeholder="Название трека"/>
        <input class="field" id="upArtist" placeholder="Исполнитель"/>
        <div class="up-btns">
          <button class="save-btn" onclick="doUpload()">Загрузить</button>
          <button class="cancel-btn" onclick="cancelUp()">Отмена</button>
        </div>
      </div>
      <div>
        <div class="sec-title" style="padding:4px 4px 10px">Загруженные треки</div>
        <div class="lib-rows" id="profLib"></div>
      </div>
      <div class="p-card" style="margin-top:12px">
        <div class="p-card-label" style="display:flex;justify-content:space-between;align-items:center">
          <span>История воспроизведения</span>
          <button class="lr-btn" style="height:28px;padding:0 10px" onclick="toggleHistory()">История</button>
        </div>
        <div class="lib-rows" id="historyList"></div>
      </div>
      <div class="p-card" style="margin-top:12px">
        <div class="p-card-label">Инструменты</div>
        <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px">
          <button class="save-btn" style="padding:10px 8px" onclick="copyGuildId()">Копировать Guild ID</button>
          <button class="save-btn" style="padding:10px 8px" onclick="pingServer()">Пинг сервера</button>
          <button class="save-btn" style="padding:10px 8px" onclick="toggleAutoRefresh(event)">Автообновление: ON</button>
          <button class="save-btn" style="padding:10px 8px" onclick="exportLibrary()">Экспорт библиотеки</button>
          <button class="save-btn" style="padding:10px 8px" onclick="clearUiCache()">Очистить UI-кэш</button>
          <button class="save-btn" style="padding:10px 8px" onclick="resetPlayerUi()">Сбросить UI плеера</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- BOTTOM STACK -->
<div class="bottom-stack">
  <div class="mini-player" id="miniPlayer" onclick="goScreen('player')">
    <img class="mini-art" id="mArt" src="" alt=""/>
    <div class="mini-info">
      <div class="mini-title" id="mTitle">—</div>
      <div class="mini-sub" id="mSub">—</div>
    </div>
    <div class="mini-btns" onclick="event.stopPropagation()">
      <button class="mini-btn" onclick="doPrev()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="19 20 9 12 19 4 19 20"/><line x1="5" y1="19" x2="5" y2="5"/></svg>
      </button>
      <button class="mini-btn" id="miniBtnPlay" onclick="togglePlay()">
        <!-- play icon by default -->
        <svg id="miniIco" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon fill="currentColor" stroke="none" points="5 3 19 12 5 21 5 3"/></svg>
      </button>
      <button class="mini-btn" onclick="doSkip()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19"/></svg>
      </button>
    </div>
    <div class="mini-prog"><div class="mini-prog-fill" id="mProg" style="width:0%"></div></div>
  </div>
  <nav class="nav">
    <button class="nav-btn active" id="nav-home" onclick="goScreen('home')">
      <svg viewBox="0 0 24 24"><path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>
      Главная
    </button>
    <button class="nav-btn" id="nav-search" onclick="goScreen('search')">
      <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      Поиск
    </button>
    <button class="nav-btn" id="nav-player" onclick="goScreen('player')">
      <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polygon points="10 8 16 12 10 16 10 8"/></svg>
      Плеер
    </button>
    <button class="nav-btn" id="nav-profile" onclick="goScreen('profile')">
      <svg viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
      Профиль
    </button>
  </nav>
</div>

<!-- QUEUE SHEET -->
<div class="overlay" id="qOverlay">
  <div class="sheet sheet-anim" style="max-height:75vh;display:flex;flex-direction:column;padding-bottom:calc(16px + var(--safe-b))">
    <div class="handle"></div>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;gap:8px;flex-shrink:0">
      <div class="sheet-title" style="margin:0">Очередь</div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="lr-btn del" onclick="clearQueue()" style="height:30px;padding:0 10px;border-radius:8px">Очистить</button>
        <button onclick="closeQueue()" style="background:none;border:none;color:var(--sub);cursor:pointer;display:flex;padding:4px">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
    </div>
    <div style="overflow-y:auto;flex:1" id="qList"></div>
  </div>
</div>

<!-- EDIT SHEET -->
<div class="overlay" id="editOverlay">
  <div class="sheet sheet-anim">
    <div class="handle"></div>
    <div class="sheet-title">Редактировать</div>
    <input class="field" id="eTitle" placeholder="Название"/>
    <input class="field" id="eArtist" placeholder="Исполнитель" style="margin-top:10px"/>
    <div class="sheet-btns" style="margin-top:14px">
      <button class="cancel-btn" onclick="closeEdit()">Отмена</button>
      <button class="save-btn" onclick="saveEdit()">Сохранить</button>
    </div>
  </div>
</div>

<div class="overlay" id="histOverlay">
  <div class="sheet sheet-anim" style="max-height:80vh;display:flex;flex-direction:column">
    <div class="handle"></div>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;flex-shrink:0">
      <div class="sheet-title" style="margin:0">Последние 30 треков</div>
      <button onclick="closeHistory()" style="background:none;border:none;color:var(--sub);cursor:pointer;display:flex;padding:4px">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div style="overflow-y:auto;flex:1" id="historyAll"></div>
  </div>
</div>

<div class="load-panel" id="loadPanel">
  <div class="lp-spinner"></div>
  <div class="lp-text">Загрузка трека...</div>
</div>

<div class="toasts" id="toasts"></div>

<script>
// ═══════════════════════════════════════════
//  STATE
// ═══════════════════════════════════════════
let guildId   = localStorage.getItem('gid') || '';
let editId    = null;
let selFile   = null;
let totalDur  = 0;
let isPlaying = false;
let isLooping = false;
let isShuffle = false;
let pollTid   = null;
let prevVol   = 50;
let lastQueueSig = '';
// Server-side elapsed — updated from /api/status
let srvElapsed = 0;
// Local elapsed interpolation
let localElapsed = 0;
let lastPollTime  = 0;
let autoRefreshOn = true;
let historyTracks = [];
window.renderThumb = function(t, cls){ return `<div class="${cls}"><div class="t-placeholder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13M9 18c0 1.1-.9 2-2 2s-2-.9-2-2 .9-2 2-2 2 .9 2 2zm12-2c0 1.1-.9 2-2 2s-2-.9-2-2 .9-2 2-2 2 .9 2 2z"/></svg></div></div>`; };

// ═══════════════════════════════════════════
//  INIT
// ═══════════════════════════════════════════
if (guildId) {
  document.getElementById('guildInput').value = guildId;
  document.getElementById('chipLabel').textContent = 'ID: ' + guildId;
} else {
  // Try fetch config if no local guildId or to sync
  fetch('/api/config').then(r=>r.json()).then(d=>{
    if(d.guild_id && d.guild_id !== guildId) {
      guildId = d.guild_id;
      localStorage.setItem('gid', guildId);
      document.getElementById('guildInput').value = guildId;
      document.getElementById('chipLabel').textContent = 'ID: ' + guildId;
    }
  }).catch(()=>{});
}
loadHomeGrid();
loadProfLib();
const savedVol = Number(localStorage.getItem('vol') || '50');
if (!Number.isNaN(savedVol)) {
  document.getElementById('volSlider').value = Math.max(0, Math.min(200, savedVol));
  document.getElementById('volVal').textContent = document.getElementById('volSlider').value + '%';
}
startPoll();

// ═══════════════════════════════════════════
//  UTILS
// ═══════════════════════════════════════════
const fmt = s => {
  s = Math.floor(s || 0);
  return Math.floor(s / 60) + ':' + (s % 60 + '').padStart(2, '0');
};

function setThumbElement(el, track, cls) {
  if (!el) return;
  const src = (track && track.thumbnail && track.thumbnail.startsWith('http')) ? track.thumbnail : '';

  const makePlaceholder = () => {
    const d = document.createElement('div');
    d.className = cls;
    d.id = el.id;
    d.innerHTML = '<div class="t-placeholder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13M9 18c0 1.1-.9 2-2 2s-2-.9-2-2 .9-2 2-2 2 .9 2 2zm12-2c0 1.1-.9 2-2 2s-2-.9-2-2 .9-2 2-2 2 .9 2 2z"/></svg></div>';
    return d;
  };

  let node;
  if (src) {
    const img = document.createElement('img');
    img.className = cls;
    img.id = el.id;
    img.alt = '';
    img.src = src;
    img.onerror = () => img.replaceWith(makePlaceholder());
    node = img;
  } else {
    node = makePlaceholder();
  }

  if (el !== node) el.replaceWith(node);
}

function toast(msg, type = 'ok') {
  const w = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.innerHTML = '<div class="t-d"></div><span>' + msg + '</span>';
  w.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .3s, transform .3s';
    el.style.opacity = '0'; el.style.transform = 'translateY(-8px)';
    setTimeout(() => el.remove(), 320);
  }, 2600);
}

// ═══════════════════════════════════════════
//  NAVIGATION
// ═══════════════════════════════════════════
function goScreen(name) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('s-' + name).classList.add('active');
  document.getElementById('nav-' + name).classList.add('active');
  if (name === 'search') setTimeout(() => document.getElementById('searchIn').focus(), 150);
}

// ═══════════════════════════════════════════
//  GUILD
// ═══════════════════════════════════════════
function applyGuild() {
  guildId = document.getElementById('guildInput').value.trim();
  localStorage.setItem('gid', guildId);
  document.getElementById('chipLabel').textContent = guildId ? 'ID: ' + guildId : 'Выбрать сервер';
  
  // Save to server
  fetch('/api/config', {
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({guild_id:guildId})
  }).catch(()=>{});

  toast('Guild ID сохранён');
  doPoll();
}

// ═══════════════════════════════════════════
//  SEARCH
// ═══════════════════════════════════════════
let sDebounce = null;
let searchQ = "";
let searchOffset = 0;
let searchHasMore = false;
let searchLoading = false;
let searchItems = [];
function onSInput() {
  const v = document.getElementById('searchIn').value;
  document.getElementById('sClear').className = 's-clear' + (v ? ' show' : '');
  clearTimeout(sDebounce);
  if (v.length > 1) sDebounce = setTimeout(doSearch, 550);
}
function clearS() {
  searchQ = ''; searchOffset = 0; searchHasMore = false; searchLoading = false; searchItems = [];
  document.getElementById('searchIn').value = '';
  document.getElementById('sClear').className = 's-clear';
  document.getElementById('sResults').innerHTML =
    '<div class="empty"><div class="empty-i"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div><p>Введи название трека<br/>или исполнителя</p></div>';
}
async function doSearch() {
  const q = document.getElementById('searchIn').value.trim();
  if (!q) return;
  searchQ = q;
  searchOffset = 0;
  searchHasMore = false;
  searchLoading = false;
  searchItems = [];
  const wrap = document.getElementById('sResults');
  wrap.innerHTML = '<div class="loader"><div class="spinner"></div>Ищем...</div>';
  await loadMoreSearch(true);
}

async function loadMoreSearch(reset = false) {
  if (!searchQ || searchLoading) return;
  if (!reset && !searchHasMore) return;
  searchLoading = true;
  try {
    const r = await fetch('/api/search?q=' + encodeURIComponent(searchQ) + '&offset=' + searchOffset + '&limit=15');
    const data = await r.json();
    const items = data.items || [];
    if (reset) searchItems = items;
    else searchItems = searchItems.concat(items);
    searchOffset = data.next_offset || (searchOffset + items.length);
    searchHasMore = !!data.has_more;
    renderSearch(searchItems, searchHasMore);
  } catch {
    toast('Ошибка поиска', 'err');
  } finally {
    searchLoading = false;
  }
}

function renderSearch(items, hasMore = false) {
  const wrap = document.getElementById('sResults');
  wrap.innerHTML = '';
  if (!items.length) {
    wrap.innerHTML = '<div class="empty"><div class="empty-i"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div><p>Ничего не найдено</p></div>';
    return;
  }
  const lib = items.filter(i => i.source === 'library');
  const yt  = items.filter(i => i.source === 'youtube');
  if (lib.length) {
    const h = document.createElement('div'); h.className = 'sec-head'; h.textContent = 'Из библиотеки'; wrap.appendChild(h);
    lib.forEach((t, i) => wrap.appendChild(mkResult(t, i)));
  }
  if (yt.length) {
    const h = document.createElement('div'); h.className = 'sec-head'; h.textContent = 'YouTube'; wrap.appendChild(h);
    yt.forEach((t, i) => wrap.appendChild(mkResult(t, i + lib.length)));
  }
  if (hasMore) {
    const more = document.createElement('div');
    more.className = 'loader';
    more.style.padding = '16px';
    more.innerHTML = '<div class="spinner"></div>Загружаем ещё...';
    wrap.appendChild(more);
  }
}

document.getElementById('sResults').addEventListener('scroll', () => {
  const el = document.getElementById('sResults');
  if (!searchHasMore || searchLoading) return;
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 180) {
    loadMoreSearch();
  }
});

function mkResult(t, idx) {
  const isLib = t.source === 'library';
  const el = document.createElement('div');
  el.className = 'result-item';
  el.style.animationDelay = (idx * .04) + 's';
  const tj = encodeURIComponent(JSON.stringify(t));
  el.innerHTML =
    window.renderThumb(t, 'r-thumb') +
    '<div class="r-info">' +
      '<div class="r-title">' + (t.title || 'Без названия') + '</div>' +
      '<div class="r-sub"><span>' + fmt(t.duration) + '</span>' +
        (t.artist ? '<span>·</span><span>' + t.artist + '</span>' : '') +
        '<span class="tag ' + (isLib ? 'tag-lib' : 'tag-yt') + '">' + (isLib ? 'Файл' : 'YouTube') + '</span>' +
      '</div>' +
    '</div>' +
    '<button class="r-play" onclick="playT(event,\'' + tj + '\', true)">' +
      '<svg viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>' +
    '</button>' +
    '<button class="r-play" onclick="playT(event,\'' + tj + '\', false)">' +
      '<svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>' +
    '</button>';
  return el;
}

// ═══════════════════════════════════════════
//  PLAY
// ═══════════════════════════════════════════
// ═══════════════════════════════════════════
//  PLAY
// ═══════════════════════════════════════════
async function playT(e, tj, isAdd) {
  e.stopPropagation();
  if (!guildId) { toast('Введи Guild ID в Профиле', 'err'); return; }
  const track = JSON.parse(decodeURIComponent(tj));
  const btn = e.currentTarget;
  
  // Show loading
  document.getElementById('loadPanel').classList.add('show');
  
  try {
    const r = await fetch('/api/play', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ guild_id: guildId, track, play_now: !isAdd })
    });
    const d = await r.json();
    if (d.error) { toast(d.error, 'err'); }
    else {
      if (isAdd) toast('Добавлено в очередь');
      else {
        toast('Запуск: ' + track.title);
        localElapsed = 0; srvElapsed = 0;
        startPoll();
        setTimeout(() => goScreen('player'), 350);
      }
    }
  } catch { toast('Ошибка', 'err'); }
  
  document.getElementById('loadPanel').classList.remove('show');
}

// ═══════════════════════════════════════════
//  CONTROLS
// ═══════════════════════════════════════════
async function ctrl(action, value) {
  if (!guildId) return;
  const b = { guild_id: guildId, action };
  if (value !== undefined) b.value = value;
  await fetch('/api/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
}
function togglePlay() { ctrl(isPlaying ? 'pause' : 'resume'); }
function doSkip()     { ctrl('skip'); }
function doPrev()     { ctrl('prev'); }
function toggleLoop() { ctrl('loop'); }
function toggleShuffle() { ctrl('shuffle'); }
function setVol(v)    {
  const safe = Math.max(0, Math.min(200, Number(v) || 0));
  document.getElementById('volVal').textContent = safe + '%';
  document.getElementById('volSlider').value = safe;
  localStorage.setItem('vol', String(safe));
  ctrl('volume', safe / 100);
}
function mute() {
  const sl = document.getElementById('volSlider');
  if (+sl.value > 0) { prevVol = sl.value; sl.value = 0; } else sl.value = prevVol;
  setVol(sl.value);
}
async function doDisconnect() { await ctrl('disconnect'); toast('Бот отключён'); }
async function clearQueue() { await ctrl('clear_queue'); toast('Очередь очищена'); doPoll(); }
async function setSleepTimer(mins) {
  await fetch('/api/control', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ guild_id: guildId, action: 'sleep_timer', minutes: mins })
  });
  toast(mins > 0 ? ('Сон через ' + mins + ' мин') : 'Таймер сна выключен');
  doPoll();
}


function copyGuildId(){
  const gid = (document.getElementById('guildInput').value || guildId || '').trim();
  if (!gid) { toast('Guild ID пуст', 'err'); return; }
  navigator.clipboard.writeText(gid).then(()=>toast('Guild ID скопирован')).catch(()=>toast('Не удалось скопировать', 'err'));
}
async function pingServer(){
  const t0 = performance.now();
  try {
    const r = await fetch('/api/config');
    if (!r.ok) throw new Error('bad');
    const ms = Math.round(performance.now() - t0);
    toast('Пинг: ' + ms + 'ms');
  } catch { toast('Сервер недоступен', 'err'); }
}
function toggleAutoRefresh(e){
  autoRefreshOn = !autoRefreshOn;
  const btn = e && e.target;
  if (btn) btn.textContent = 'Автообновление: ' + (autoRefreshOn ? 'ON' : 'OFF');
  toast(autoRefreshOn ? 'Автообновление включено' : 'Автообновление выключено');
}
async function exportLibrary(){
  try {
    const r = await fetch('/api/library');
    const data = await r.json();
    const blob = new Blob([JSON.stringify(data, null, 2)], {type:'application/json'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'library-export.json';
    document.body.appendChild(a); a.click(); a.remove();
    toast('Экспорт готов');
  } catch { toast('Ошибка экспорта', 'err'); }
}
function clearUiCache(){
  localStorage.removeItem('vol');
  localStorage.removeItem('gid');
  toast('UI-кэш очищен');
}
function resetPlayerUi(){
  document.getElementById('progFill').style.width = '0%';
  document.getElementById('mProg').style.width = '0%';
  document.getElementById('tNow').textContent = '0:00';
  toast('UI плеера сброшен');
}


function renderHistory(limit = 5){
  const list = document.getElementById('historyList');
  const all = document.getElementById('historyAll');
  if (!list || !all) return;
  const arr = (historyTracks || []);
  if (!arr.length){
    const empty = '<div class="empty" style="padding:14px 0"><p>История пока пуста</p></div>';
    list.innerHTML = empty; all.innerHTML = empty; return;
  }
  const mk = (t, i) => '<div class="lib-row" style="padding:6px 0"><span class="q-n">'+(i+1)+'</span>'+window.renderThumb(t,'lr-art')+'<div class="lr-info"><div class="lr-title">'+(t.title||'Без названия')+'</div><div class="lr-sub">'+(t.artist||'—')+'</div></div></div>';
  list.innerHTML = arr.slice(0, limit).map((t,i)=>mk(t,i)).join('');
  all.innerHTML = arr.map((t,i)=>mk(t,i)).join('');
}
function toggleHistory(){ document.getElementById('histOverlay').classList.add('show'); }
function closeHistory(){ document.getElementById('histOverlay').classList.remove('show'); }
document.getElementById('histOverlay').addEventListener('click', e => { if (e.target === e.currentTarget) closeHistory(); });

async function sendTTS() {
  if (!guildId) { toast('Введи Guild ID в Профиле', 'err'); return; }
  const text = document.getElementById('ttsText').value.trim();
  if (!text) { toast('Введите текст', 'err'); return; }
  const r = await fetch('/api/tts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ guild_id: guildId, text, lang: 'ru' })
  });
  const d = await r.json();
  if (d.error) toast(d.error, 'err');
  else { toast('Озвучка добавлена в очередь'); document.getElementById('ttsText').value = ''; doPoll(); }
}

// ═══════════════════════════════════════════
//  SEEK — progress bar click/touch
// ═══════════════════════════════════════════
let isSeeking = false;
function initSeekBar() {
  const bar = document.getElementById('progBar');
  function seekFromEvent(e) {
    const rect = bar.getBoundingClientRect();
    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    const pct = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    const pos = Math.floor(pct * totalDur);
    document.getElementById('progFill').style.width = (pct * 100) + '%';
    document.getElementById('tNow').textContent = fmt(pos);
    return pos;
  }
  function doSeekRequest(pos) {
    if (!guildId || totalDur <= 0) return;
    fetch('/api/seek', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ guild_id: guildId, position: pos })
    }).then(r => r.json()).then(d => {
      if (d.error) toast(d.error, 'err');
      else { srvElapsed = pos; lastPollTime = Date.now(); }
    }).catch(() => toast('Ошибка перемотки', 'err'));
  }
  // Mouse
  bar.addEventListener('mousedown', e => {
    isSeeking = true;
    const pos = seekFromEvent(e);
    function onMove(e2) { seekFromEvent(e2); }
    function onUp(e2) {
      isSeeking = false;
      const finalPos = seekFromEvent(e2);
      doSeekRequest(finalPos);
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
  // Touch
  bar.addEventListener('touchstart', e => {
    isSeeking = true;
    seekFromEvent(e);
  }, { passive: true });
  bar.addEventListener('touchmove', e => {
    if (isSeeking) seekFromEvent(e);
  }, { passive: true });
  bar.addEventListener('touchend', e => {
    if (!isSeeking) return;
    isSeeking = false;
    const rect = bar.getBoundingClientRect();
    const touch = e.changedTouches[0];
    const pct = Math.max(0, Math.min(1, (touch.clientX - rect.left) / rect.width));
    const pos = Math.floor(pct * totalDur);
    doSeekRequest(pos);
  });
}
initSeekBar();

// ═══════════════════════════════════════════
//  KEYBOARD SHORTCUTS
// ═══════════════════════════════════════════
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
  else if (e.code === 'ArrowRight') { e.preventDefault(); seekRelative(10); }
  else if (e.code === 'ArrowLeft')  { e.preventDefault(); seekRelative(-10); }
  else if (e.code === 'ArrowUp')    { e.preventDefault(); adjustVol(10); }
  else if (e.code === 'ArrowDown')  { e.preventDefault(); adjustVol(-10); }
});
function seekRelative(delta) {
  if (!guildId || totalDur <= 0) return;
  const secSincePoll = (Date.now() - lastPollTime) / 1000;
  const current = Math.floor(srvElapsed + (isPlaying ? secSincePoll : 0));
  const pos = Math.max(0, Math.min(totalDur, current + delta));
  fetch('/api/seek', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ guild_id: guildId, position: pos })
  }).then(r => r.json()).then(d => {
    if (!d.error) { srvElapsed = pos; lastPollTime = Date.now(); }
  }).catch(() => {});
}
function adjustVol(delta) {
  const sl = document.getElementById('volSlider');
  sl.value = Math.max(0, Math.min(200, +sl.value + delta));
  setVol(sl.value);
}

// ═══════════════════════════════════════════
//  POLL — server gives exact elapsed
// ═══════════════════════════════════════════
function startPoll() {
  if (pollTid) return;
  pollTid = setInterval(doPoll, 2000);
  doPoll();
}

async function doPoll() {
  if (!guildId || !autoRefreshOn) return;
  try {
    const r = await fetch('/api/status?guild_id=' + guildId);
    const s = await r.json();

    isPlaying = s.playing;
    isLooping = s.loop;
    srvElapsed = s.elapsed || 0;
    lastPollTime = Date.now();

    const srvVol = Math.round((Number(s.volume || 0.5)) * 100);
    const volSlider = document.getElementById('volSlider');
    if (Number(volSlider.value) !== srvVol) {
      volSlider.value = srvVol;
      document.getElementById('volVal').textContent = srvVol + '%';
      localStorage.setItem('vol', String(srvVol));
    }
    
    const sleepEl = document.getElementById('sleepInfo');
    if (sleepEl) {
      sleepEl.textContent = s.sleep_remaining > 0 ? ('Сон: ' + fmt(s.sleep_remaining)) : 'Сон: выкл';
    }

    // Help function for thumbnails
    const escAttr = v => String(v || '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const getThumb = (t, cls) => {
      if (t.thumbnail && t.thumbnail.startsWith('http')) {
        const safeSrc = escAttr(t.thumbnail);
        return `<img class="${cls}" src="${safeSrc}" loading="lazy" />`;
      }
      return getThumbPlaceholder(cls);
    };
    const getThumbPlaceholder = (cls) => {
      return `<div class="${cls}"><div class="t-placeholder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13M9 18c0 1.1-.9 2-2 2s-2-.9-2-2 .9-2 2-2 2 .9 2 2zm12-2c0 1.1-.9 2-2 2s-2-.9-2-2 .9-2 2-2 2 .9 2 2z"/></svg></div></div>`;
    };
    window.renderThumb = getThumb; // make it global for other functions

    // Sync local elapsed with server value
    localElapsed = srvElapsed;

    // Status dots
    const cd = document.getElementById('chipDot');
    const pd = document.getElementById('pDot');
    const pt = document.getElementById('pStatus');
    if (!s.connected) {
      cd.className = 'chip-dot'; pd.className = 's-dot'; pt.textContent = 'Не подключён';
    } else if (s.playing) {
      cd.className = 'chip-dot playing'; pd.className = 's-dot pl'; pt.textContent = 'Играет';
    } else {
      cd.className = 'chip-dot on'; pd.className = 's-dot on'; pt.textContent = 'Подключён';
    }

    // Play button state — use CSS class instead of swapping innerHTML
    const playBtn = document.getElementById('btnPlay');
    if (s.playing) { playBtn.classList.add('playing'); }
    else           { playBtn.classList.remove('playing'); }

    // Mini play icon
    const miniIco = document.getElementById('miniIco');
    if (s.playing) {
      miniIco.innerHTML = '<rect x="5" y="4" width="4" height="16" fill="currentColor" rx="1"/><rect x="15" y="4" width="4" height="16" fill="currentColor" rx="1"/>';
    } else {
      miniIco.innerHTML = '<polygon fill="currentColor" stroke="none" points="5 3 19 12 5 21 5 3"/>';
    }

    // Loop & Shuffle buttons
    document.getElementById('btnLoop').className = 'c-side' + (s.loop ? ' on' : '');
    document.getElementById('btnShuffle').className = 'c-side' + (s.shuffle ? ' on' : '');
    isShuffle = s.shuffle;

    historyTracks = s.history || [];
    renderHistory(5);

    // Current track
    const currentUi = s.current || null;
    if (currentUi) {
      const t = currentUi;
      document.getElementById('pTitle').textContent  = t.title  || 'Без названия';
      document.getElementById('pArtist').textContent = t.artist || '—';
      document.getElementById('mTitle').textContent  = t.title  || '—';
      document.getElementById('mSub').textContent    = t.artist || '—';
      
      setThumbElement(document.getElementById('pArt'), t, 'p-art' + (s.playing ? ' lit' : ''));
      setThumbElement(document.getElementById('mArt'), t, 'mini-art');

      totalDur = t.duration || 0;
      document.getElementById('tEnd').textContent = totalDur > 0 ? fmt(totalDur) : '—:—';
      document.getElementById('tNow').textContent = fmt(srvElapsed);

      // Show mini player ONLY when on non-player screens
      const onPlayer = document.getElementById('s-player').classList.contains('active');
      if (!onPlayer) {
        document.getElementById('miniPlayer').classList.add('visible');
      } else {
        document.getElementById('miniPlayer').classList.remove('visible');
      }
    } else {
      document.getElementById('pTitle').textContent  = 'Ничего не играет';
      document.getElementById('pArtist').textContent = '—';
      document.getElementById('miniPlayer').classList.remove('visible');
      totalDur = 0;
      localElapsed = 0;
      document.getElementById('tNow').textContent = '0:00';
      document.getElementById('tEnd').textContent = '0:00';
      document.getElementById('progFill').style.width = '0%';
      document.getElementById('mProg').style.width = '0%';
    }

    const queueSig = JSON.stringify((s.queue || []).map(t => [t.title, t.duration, t.artist]));
    if (queueSig !== lastQueueSig) {
      renderQueue(s.queue || []);
      lastQueueSig = queueSig;
    }
  } catch (e) { console.error('poll error', e); }
}

// ── Hide mini-player when on player screen (called on nav switch)
function syncMiniPlayer() {
  const onPlayer = document.getElementById('s-player').classList.contains('active');
  const mini = document.getElementById('miniPlayer');
  if (onPlayer) mini.classList.remove('visible');
  else if (document.getElementById('pTitle').textContent !== 'Ничего не играет') mini.classList.add('visible');
}

// Override goScreen to also sync mini-player
const _goScreen = goScreen;
window.goScreen = function(name) {
  _goScreen(name);
  syncMiniPlayer();
};

// ── Progress ticker — interpolates between polls
setInterval(() => {
  if (!isPlaying || isSeeking) return;
  const secSincePoll = (Date.now() - lastPollTime) / 1000;
  const displayed = Math.floor(srvElapsed + secSincePoll);
  document.getElementById('tNow').textContent = fmt(displayed);
  if (totalDur > 0) {
    const pct = Math.min(100, (displayed / totalDur) * 100);
    document.getElementById('progFill').style.width  = pct + '%';
    document.getElementById('mProg').style.width     = pct + '%';
  }
}, 500);

// ═══════════════════════════════════════════
//  QUEUE
// ═══════════════════════════════════════════
function renderQueue(queue) {
  const list = document.getElementById('qList');
  list.innerHTML = '';
  if (!queue.length) {
    list.innerHTML = '<div class="empty" style="padding:28px 0"><div class="empty-i"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/></svg></div><p>Очередь пуста</p></div>';
    return;
  }
  queue.forEach((t, i) => {
    const el = document.createElement('div');
    el.className = 'q-item';
    el.style.animationDelay = (i * .03) + 's';
    el.innerHTML =
      '<span class="q-n">' + (i + 1) + '</span>' +
      window.renderThumb(t, 'q-thumb') +
      '<div class="q-info"><div class="q-title">' + t.title + '</div><div class="q-dur">' + fmt(t.duration) + '</div></div>' +
      '<div style="display:flex;flex-direction:column;gap:2px;margin-right:6px">' + 
        '<button class="lr-btn" onclick="moveQ(' + i + ',-1)" style="width:24px;height:24px"><svg viewBox="0 0 24 24"><polyline points="18 15 12 9 6 15"/></svg></button>' +
        '<button class="lr-btn" onclick="moveQ(' + i + ',1)" style="width:24px;height:24px"><svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg></button>' +
      '</div>' +
      '<button class="lr-btn del" onclick="removeFromQueue(' + i + ')" style="flex-shrink:0">' +
        '<svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
      '</button>';
    list.appendChild(el);
  });
}
async function moveQ(from, dir) {
  if (!guildId) return;
  const to = from + dir;
  await fetch('/api/queue/move', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ guild_id: guildId, from, to })
  });
  doPoll();
}
async function removeFromQueue(idx) {
  if (!guildId) return;
  const r = await fetch('/api/queue/remove', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ guild_id: guildId, index: idx })
  });
  const d = await r.json();
  if (d.error) toast(d.error, 'err');
  else { toast('Удалено из очереди'); doPoll(); }
}
function openQueue()  { document.getElementById('qOverlay').classList.add('show'); }
function closeQueue() { document.getElementById('qOverlay').classList.remove('show'); }
document.getElementById('qOverlay').addEventListener('click', e => { if (e.target === e.currentTarget) closeQueue(); });

// ═══════════════════════════════════════════
//  LIBRARY — HOME GRID
// ═══════════════════════════════════════════
async function loadHomeGrid() {
  const r = await fetch('/api/library');
  const lib = await r.json();
  const grid = document.getElementById('homeGrid');
  grid.innerHTML = '';
  if (!lib.length) {
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1;padding:32px 0"><div class="empty-i"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13M9 18c0 1.1-.9 2-2 2s-2-.9-2-2 .9-2 2-2 2 .9 2 2zm12-2c0 1.1-.9 2-2 2s-2-.9-2-2 .9-2 2-2 2 .9 2 2z"/></svg></div><p>Библиотека пуста.<br/>Добавь треки в Профиле</p></div>';
    return;
  }
  lib.forEach((t, i) => {
    const el = document.createElement('div');
    el.className = 'lib-card';
    el.style.animationDelay = (i * .06) + 's';
    const tj = encodeURIComponent(JSON.stringify(t));
    el.innerHTML =
      window.renderThumb(t, 'lib-card-art') +
      '<div class="lib-card-body">' +
        '<div class="lib-card-title">' + (t.title || 'Без названия') + '</div>' +
        '<div class="lib-card-sub">' + (t.artist || '—') + ' · ' + fmt(t.duration) + '</div>' +
      '</div>';
    el.addEventListener('click', e => playT(e, tj));
    grid.appendChild(el);
  });
}

// ═══════════════════════════════════════════
//  LIBRARY — PROFILE LIST
// ═══════════════════════════════════════════
async function loadProfLib() {
  const r = await fetch('/api/library');
  const lib = await r.json();
  const list = document.getElementById('profLib');
  list.innerHTML = '';
  if (!lib.length) {
    list.innerHTML = '<div class="empty" style="padding:24px 0"><div class="empty-i"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg></div><p>Нет загруженных треков</p></div>';
    return;
  }
  lib.forEach((t, i) => {
    const el = document.createElement('div');
    el.className = 'lib-row';
    el.style.animationDelay = (i * .04) + 's';
    const tj = encodeURIComponent(JSON.stringify(t));
    el.innerHTML =
      window.renderThumb(t, 'lr-art') +
      '<div class="lr-info">' +
        '<div class="lr-title">' + (t.title || 'Без названия') + '</div>' +
        '<div class="lr-sub">' + (t.artist || '—') + ' · ' + fmt(t.duration) + '</div>' +
      '</div>' +
      '<div class="lr-btns">' +
        '<button class="lr-btn" onclick="playT(event,\'' + tj + '\')">' +
          '<svg viewBox="0 0 24 24"><polygon fill="currentColor" stroke="none" points="5 3 19 12 5 21 5 3"/></svg>' +
        '</button>' +
        '<button class="lr-btn" onclick="openEdit(\'' + t.id + '\',\'' + encodeURIComponent(t.title||'') + '\',\'' + encodeURIComponent(t.artist||'') + '\',\'' + encodeURIComponent(t.thumbnail||'') + '\')">' +
          '<svg viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>' +
        '</button>' +
        '<button class="lr-btn del" onclick="delTrack(\'' + t.id + '\')">' +
          '<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>' +
        '</button>' +
      '</div>';
    list.appendChild(el);
  });
}

async function delTrack(id) {
  if (!confirm('Удалить трек?')) return;
  await fetch('/api/library/' + id, { method: 'DELETE' });
  toast('Удалено');
  loadHomeGrid();
  loadProfLib();
}

// ── EDIT
function openEdit(id, t, a, th) {
  editId = id;
  document.getElementById('eTitle').value  = decodeURIComponent(t);
  document.getElementById('eArtist').value = decodeURIComponent(a);
  document.getElementById('eThumb').value  = decodeURIComponent(th);
  document.getElementById('editOverlay').classList.add('show');
}
function closeEdit() { document.getElementById('editOverlay').classList.remove('show'); editId = null; }
document.getElementById('editOverlay').addEventListener('click', e => { if (e.target === e.currentTarget) closeEdit(); });
async function saveEdit() {
  if (!editId) return;
  await fetch('/api/library/' + editId, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: document.getElementById('eTitle').value, artist: document.getElementById('eArtist').value, thumbnail: document.getElementById('eThumb').value })
  });
  toast('Обновлено'); closeEdit(); loadHomeGrid(); loadProfLib();
}

// ═══════════════════════════════════════════
//  UPLOAD
// ═══════════════════════════════════════════
function onFile() {
  selFile = document.getElementById('fileIn').files[0];
  if (!selFile) return;
  document.getElementById('upTitle').value  = selFile.name.replace(/\.[^.]+$/, '');
  document.getElementById('upArtist').value = '';
  document.getElementById('upFields').classList.add('show');
}
function cancelUp() {
  selFile = null;
  document.getElementById('fileIn').value = '';
  document.getElementById('upFields').classList.remove('show');
}
async function doUpload() {
  if (!selFile) { toast('Файл не выбран', 'err'); return; }
  
  // Client-side size check (100MB)
  if (selFile.size > 100 * 1024 * 1024) {
    toast('Файл слишком большой (лимит 100МБ)', 'err');
    console.warn('File size limit exceeded:', selFile.size);
    return;
  }
  
  console.log('Starting upload:', selFile.name, selFile.size, 'bytes');
  
  const fd = new FormData();
  fd.append('file',   selFile);
  fd.append('title',  document.getElementById('upTitle').value  || selFile.name);
  fd.append('artist', document.getElementById('upArtist').value || 'Неизвестен');
  
  // Instant feedback
  toast('Начинаю загрузку...');
  
  // Show indicator
  const lp = document.getElementById('loadPanel');
  lp.querySelector('.lp-text').textContent = 'Загрузка файла...';
  lp.classList.add('show');
  
  try {
    console.log('Fetching /api/upload...');
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    if (!r.ok) throw new Error('Server returned ' + r.status);
    
    const d = await r.json();
    console.log('Upload response:', d);
    if (d.error) { toast(d.error, 'err'); }
    else {
      toast('Трек успешно добавлен');
      cancelUp();
      loadHomeGrid();
      loadProfLib();
    }
  } catch (e) {
    console.error('Upload failed:', e);
    toast('Ошибка загрузки: ' + e.message, 'err');
  } finally {
    lp.classList.remove('show');
    setTimeout(() => {
      lp.querySelector('.lp-text').textContent = 'Загрузка трека...';
    }, 400);
  }
}
</script>
</body>
</html>"""

@app.route("/")
def root():
    return Response(HTML, mimetype="text/html")

def run_flask():
    print("[*] Web: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    bot.run(DISCORD_TOKEN)
