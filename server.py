"""
Korea-English Meeting Translator v1.0 (Refactored from Laos-Korea v8)
"""

import os
import subprocess
import platform
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import anthropic

app = Flask(__name__, static_folder="static")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "ko-en-meeting-2026")
CORS(app, resources={r"/*": {"origins": "*"}})

# Render 환경에서 복잡한 eventlet 워커 호환성 문제를 피하기 위해 내장 threading 모드 최적화 구동
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    ping_timeout=60, ping_interval=25)

anthropic_client = None
meetings = {}

def get_or_create_meeting(meeting_id):
    if meeting_id not in meetings:
        meetings[meeting_id] = {"entries": [], "users": {}, "context_docs": []}
    return meetings[meeting_id]

def init_clients():
    global anthropic_client
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    print(f"[init] ANTHROPIC_API_KEY length: {len(api_key)}")
    if api_key:
        try:
            anthropic_client = anthropic.Anthropic(api_key=api_key)
            print("[init] Anthropic API OK")
        except Exception as e:
            print(f"[init] Anthropic init error: {e}")
    else:
        print("[init] ANTHROPIC_API_KEY not set")

def translate(text, source_lang, context_docs=None):
    if not anthropic_client:
        return {"error": "ANTHROPIC_API_KEY not set",
                "source_lang": source_lang, "target_lang": "",
                "source_text": text, "translated_text": ""}
    
    # 대상을 라오스어(lo)에서 영어(en)로 전면 수정
    target_lang = "en" if source_lang == "ko" else "ko"
    lang_names  = {"ko": "Korean", "en": "English"}

    # 참조문서 컨텍스트 구성
    context_block = ""
    if context_docs:
        context_block = "\n[Reference Documents]\n"
        for doc in context_docs:
            context_block += f"--- {doc['name']} ---\n{doc['content'][:3000]}\n\n"
        context_block += (
            "Use the above documents to ensure accurate terminology and context.\n"
            "When translating, prioritize terms and names from these documents.\n"
        )

    # 비즈니스 회의 목적에 맞는 한-영 전문 통역 프롬프트 개조
    prompt = (
        "You are a professional interpreter for Korean-English business meetings.\n"
        + context_block +
        "Translate the following " + lang_names[source_lang] + " text to " + lang_names[target_lang] + ".\n"
        "Output the translation only. No explanation, no quotes, no extra text.\n\n"
        "Text:\n" + text
    )
    try:
        msg = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        return {
            "source_lang": source_lang, "source_text": text,
            "target_lang": target_lang,
            "translated_text": msg.content[0].text.strip(),
            "error": ""
        }
    except Exception as e:
        return {"error": str(e), "source_lang": source_lang,
                "source_text": text, "target_lang": target_lang, "translated_text": ""}

# ── HTTP routes ────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/status")
def status():
    return jsonify({"anthropic": anthropic_client is not None, "version": "v1.0_en"})

@app.route("/api/translate", methods=["POST"])
def api_translate():
    data = request.get_json()
    text = data.get("text", "").strip()
    source_lang = data.get("source_lang", "ko")
    if not text:
        return jsonify({"error": "No text provided"}), 400
    return jsonify(translate(text, source_lang))

@app.route("/api/minutes/<meeting_id>")
def get_minutes(meeting_id):
    meeting = meetings.get(meeting_id)
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404
    return jsonify({
        "meeting_id": meeting_id,
        "saved_at": datetime.now().isoformat(),
        "entry_count": len(meeting["entries"]),
        "entries": meeting["entries"]
    })

@app.route("/api/context/<meeting_id>", methods=["GET"])
def get_context(meeting_id):
    meeting = get_or_create_meeting(meeting_id)
    docs = meeting.get("context_docs", [])
    return jsonify({
        "meeting_id": meeting_id,
        "docs": [{"name": d["name"], "size": len(d["content"])} for d in docs]
    })

@app.route("/api/context/<meeting_id>", methods=["POST"])
def add_context(meeting_id):
    data    = request.get_json()
    name    = data.get("name", "Document")
    content_text = data.get("content", "").strip()
    if not content_text:
        return jsonify({"error": "Empty content"}), 400
    meeting = get_or_create_meeting(meeting_id)
    meeting["context_docs"] = [d for d in meeting["context_docs"] if d["name"] != name]
    meeting["context_docs"].append({"name": name, "content": content_text})
    socketio.emit("context_updated", {
        "docs": [{"name": d["name"], "size": len(d["content"])} for d in meeting["context_docs"]]
    }, room=meeting_id)
    print(f"[context] [{meeting_id}] Added: {name} ({len(content_text)} chars)")
    return jsonify({"success": True, "doc_count": len(meeting["context_docs"])})

@app.route("/api/context/<meeting_id>/<doc_name>", methods=["DELETE"])
def delete_context(meeting_id, doc_name):
    meeting = get_or_create_meeting(meeting_id)
    meeting["context_docs"] = [d for d in meeting["context_docs"] if d["name"] != doc_name]
    socketio.emit("context_updated", {
        "docs": [{"name": d["name"], "size": len(d["content"])} for d in meeting["context_docs"]]
    }, room=meeting_id)
    return jsonify({"success": True})

@app.route("/api/save_minutes", methods=["POST"])
def save_minutes():
    data         = request.get_json()
    meeting_id   = data.get("meeting_id", "meeting")
    text_content = data.get("content", "")
    save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "minutes")
    os.makedirs(save_dir, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = meeting_id + "_" + ts + ".txt"
    filepath = os.path.join(save_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(text_content)
    print("[saved] " + filepath)
    return jsonify({"success": True, "filename": filename, "filepath": filepath})

@app.route("/api/open_file", methods=["POST"])
def open_file():
    data     = request.get_json()
    filepath = data.get("filepath", "")
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404
    try:
        if platform.system() == "Windows":
            subprocess.Popen(["notepad.exe", filepath])
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", "-e", filepath])
        else:
            subprocess.Popen(["xdg-open", filepath])
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/summary/<meeting_id>")
def get_summary(meeting_id):
    meeting = meetings.get(meeting_id)
    if not meeting or not meeting["entries"]:
        return jsonify({"error": "No conversation found"}), 404
    entries = meeting["entries"]
    lines = []
    for e in entries:
        ko_text = e["translated_text"] if e["source_lang"] == "en" else e["source_text"]
        speaker = "[" + e["speaker"] + "] " if e.get("speaker") else ""
        if ko_text:
            lines.append(speaker + ko_text)
    conversation = "\n".join(lines)
    prompt = (
        "The following is the transcript of a Korea-US business meeting.\n"
        "Excluding greetings and procedural remarks, please provide a concise summary "
        "of the practical discussions, decisions made, and items requiring follow-up.\n"
        "Indicate the speaker if applicable. Please write the summary in Korean.\n\n"
        "Meeting Transcript:\n" + conversation
    )
    try:
        msg = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        summary_text = msg.content[0].text.strip()
        now  = datetime.now().strftime("%Y-%m-%d %H:%M")
        full = (
            "================================================\n"
            "Meeting Summary: " + meeting_id + "\n"
            "Generated: " + now + "\n"
            "================================================\n\n"
            + summary_text + "\n"
        )
        return jsonify({"summary": full, "entry_count": len(entries)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

init_clients()

# ── SocketIO events ────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    print("[connect] " + request.sid)

@socketio.on("disconnect")
def on_disconnect():
    for mid, meeting in meetings.items():
        if request.sid in meeting["users"]:
            role = meeting["users"].pop(request.sid, "unknown")
            emit("user_left", {"role": role, "user_count": len(meeting["users"])}, room=mid)
            leave_room(mid)
            break

@socketio.on("join")
def on_join(data):
    meeting_id = data.get("meeting_id", "meeting1")
    role       = data.get("role", "korean")
    meeting = get_or_create_meeting(meeting_id)
    meeting["users"][request.sid] = role
    join_room(meeting_id)
    emit("joined", {
        "meeting_id": meeting_id, "role": role,
        "user_count": len(meeting["users"]),
        "history": meeting["entries"],
        "context_docs": [{"name": d["name"], "size": len(d["content"])} for d in meeting.get("context_docs", [])]
    })
    emit("user_joined", {"role": role, "user_count": len(meeting["users"])},
         room=meeting_id, include_self=False)

@socketio.on("send_message")
def on_send_message(data):
    import threading
    meeting_id  = data.get("meeting_id", "meeting1")
    source_lang = data.get("source_lang", "ko")
    source_text = data.get("source_text", "").strip()
    speaker     = data.get("speaker", "")
    if not source_text:
        return

    meeting    = get_or_create_meeting(meeting_id)
    entry_id   = len(meeting["entries"]) + 1
    entry_time = datetime.now().strftime("%H:%M:%S")
    target_lang = "en" if source_lang == "ko" else "ko"

    socketio.emit("message_pending", {
        "id": entry_id, "time": entry_time,
        "source_lang": source_lang, "source_text": source_text,
        "target_lang": target_lang, "translated_text": None,
        "speaker": speaker, "error": ""
    }, room=meeting_id)

    def do_translate():
        context_docs = meeting.get("context_docs", [])
        result = translate(source_text, source_lang, context_docs)
        entry = {
            "id": entry_id, "time": entry_time,
            "source_lang": source_lang, "source_text": source_text,
            "target_lang": result.get("target_lang", target_lang),
            "translated_text": result.get("translated_text", ""),
            "speaker": speaker, "error": result.get("error", "")
        }
        meeting["entries"].append(entry)
        socketio.emit("message_done", entry, room=meeting_id)

    threading.Thread(target=do_translate, daemon=True).start()

@socketio.on("clear_meeting")
def on_clear(data):
    meeting_id = data.get("meeting_id", "meeting1")
    if meeting_id in meetings:
        meetings[meeting_id]["entries"] = []
    emit("meeting_cleared", {}, room=meeting_id)

if __name__ == "__main__":
    init_clients()
    port = int(os.environ.get("PORT", 5000))
    print("\nKorea-English Meeting Translator v1.0")
    print("Korean : http://localhost:" + str(port) + "/?role=korean&meeting=meeting1")
    print("English: http://localhost:" + str(port) + "/?role=english&meeting=meeting1\n")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)