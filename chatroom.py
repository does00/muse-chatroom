#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
muse-chatroom: 极简聊天室，给人 + AI 机器人用。
- 纯标准库，无第三方依赖
- 轮询 API（机器人友好）：GET /api/messages?room=&since=&nick= ；POST /api/send
- 网页 UI：/ 或 /?room=xxx
环境变量：PORT（默认 3007），DATA_DIR（默认 ./data）
"""
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", "3007"))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
MAX_KEEP = 500        # 每个房间内存保留消息数
MAX_TEXT = 2000       # 单条消息最大字符数
MAX_UPLOAD = 10 * 1024 * 1024  # 图片上传上限 10MB
PRESENCE_TTL = int(os.environ.get("PRESENCE_TTL", "90"))  # 在线判定秒数
RATE_WINDOW = 10      # 限流窗口秒
RATE_MAX = 8          # 窗口内最多消息数（按 IP）

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)


def img_ext(data):
    """按 magic bytes 识别图片类型，返回扩展名；非图片返回 None"""
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None

_rooms = {}
_rooms_lock = threading.Lock()
_rate = {}  # ip -> [timestamps]
# 房间密码：{room: password}，存硬盘；空=不设密码
_PASSWORDS_FILE = os.path.join(DATA_DIR, "passwords.json")
def _passwords_load():
    try:
        with open(_PASSWORDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k)[:32]: str(v)[:64] for k, v in data.items()}
    except Exception:
        pass
    return {}
def _passwords_save(d):
    try:
        tmp = _PASSWORDS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _PASSWORDS_FILE)
    except Exception:
        pass
def _check_room_pass(room, provided):
    pw = _passwords_load().get(room, "")
    return (not pw) or (provided == pw)
# Muse 分身人设：{nick: persona}，存硬盘，管理页可编辑，hook worker 实时拉取
_PERSONAS_FILE = os.path.join(DATA_DIR, "personas.json")
def _personas_load():
    try:
        with open(_PERSONAS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k)[:20]: str(v)[:2000] for k, v in data.items()
                    if re.fullmatch(r"[\w\-\u4e00-\u9fff]{1,20}", str(k)[:20])}
    except Exception:
        pass
    return {}
def _personas_save(d):
    try:
        tmp = _PERSONAS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _PERSONAS_FILE)
    except Exception:
        pass
# 设置同步：code -> {"data": {"nick":..., "remarks":{...}}}
# 存硬盘 sync.json，永久有效，重启不丢；超 500 条删最早的
_SYNC_FILE = os.path.join(DATA_DIR, "sync.json")
_SYNC_MAX = 500
def _sync_load():
    try:
        with open(_SYNC_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {c: it for c, it in list(data.items())[-_SYNC_MAX:]
                    if isinstance(it, dict) and isinstance(it.get("data"), dict)}
    except Exception:
        pass
    return {}
def _sync_save():
    try:
        tmp = _SYNC_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_sync_store, f, ensure_ascii=False)
        os.replace(tmp, _SYNC_FILE)
    except Exception:
        pass
_sync_store = _sync_load()
_sync_lock = threading.Lock()


def safe_room(name):
    name = (name or "lobby").strip()[:32]
    if not re.fullmatch(r"[\w\-]+", name):
        return "lobby"
    return name


def safe_nick(nick):
    nick = (nick or "").strip()[:20]
    if not nick or not re.fullmatch(r"[\w\-\u4e00-\u9fff]+", nick):
        return None
    return nick


def get_room(name):
    name = safe_room(name)
    with _rooms_lock:
        r = _rooms.get(name)
        if r is None:
            r = {"messages": [], "seq": 0, "presence": {}, "seen": {}}
            p = os.path.join(DATA_DIR, name + ".jsonl")
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        lines = f.readlines()[-MAX_KEEP:]
                    for ln in lines:
                        try:
                            m = json.loads(ln)
                        except Exception:
                            continue
                        if isinstance(m.get("id"), int):
                            r["seq"] = max(r["seq"], m["id"])
                            mm = {
                                "id": m["id"], "nick": str(m.get("nick", ""))[:20],
                                "text": str(m.get("text", ""))[:MAX_TEXT],
                                "ts": int(m.get("ts", 0)),
                            }
                            if m.get("img"):
                                mm["img"] = str(m["img"])[:64]
                            try:
                                rt = int(m.get("reply_to") or 0)
                            except (TypeError, ValueError):
                                rt = 0
                            if rt > 0:
                                mm["reply_to"] = rt
                            r["messages"].append(mm)
                            nk = str(m.get("nick", ""))[:20]
                            if nk:
                                r["seen"][nk] = max(r["seen"].get(nk, 0), int(m.get("ts", 0)))
                    r["messages"] = r["messages"][-MAX_KEEP:]
                except Exception:
                    pass
            _rooms[name] = r
        return r


def persist(room_name, msg):
    try:
        with open(os.path.join(DATA_DIR, room_name + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    except Exception:
        pass


def prune_presence(room):
    now = time.time()
    for nick in [n for n, t in room["presence"].items() if now - t > PRESENCE_TTL]:
        del room["presence"][nick]


def check_rate(ip):
    now = time.time()
    ts = _rate.get(ip, [])
    ts = [t for t in ts if now - t < RATE_WINDOW]
    if len(ts) >= RATE_MAX:
        return False
    ts.append(now)
    _rate[ip] = ts
    return True


HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Muse 聊天室</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f1419;color:#e7e9ea;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;height:100vh;display:flex;flex-direction:column}
header{background:#15202b;border-bottom:1px solid #2f3336;padding:10px 14px}
#headinner{display:flex;align-items:center;gap:10px;width:100%;max-width:1024px;margin:0 auto}
header .room{font-weight:700;font-size:17px}
header .online{font-size:13px;color:#8b98a5}
#users{font-size:12px;color:#8b98a5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;text-align:right}
#msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:85%;line-height:1.5}
.msg .meta{font-size:13px;color:#8b98a5;margin-bottom:2px}
.msg .bubble{background:#1d2a38;border-radius:12px;padding:8px 12px;word-break:break-word;white-space:pre-wrap;font-size:17px}
.msg.me{align-self:flex-end}
.msg.me .bubble{background:#1d6fd1}
.msg.sys{align-self:center}
.msg.sys .bubble{background:transparent;color:#8b98a5;font-size:14px}
#bar{border-top:1px solid #2f3336;padding:10px;background:#15202b}
#barinner{display:flex;gap:8px;width:100%;max-width:1024px;margin:0 auto}
#wrap{flex:1;display:flex;min-height:0;width:100%;max-width:1024px;margin:0 auto}
#side{width:212px;flex:none;background:#15202b;border-left:1px solid #2f3336;padding:12px;overflow-y:auto}
#side h3{font-size:14px;color:#8b98a5;margin-bottom:6px;font-weight:600}
#mcount{font-weight:400}
#memberlist{list-style:none}
#memberlist li{display:flex;align-items:center;gap:8px;padding:7px 2px;font-size:14px;border-bottom:1px solid #1c2733}
.dot{width:8px;height:8px;border-radius:50%;background:#1d9bf0;flex:none}
#memberlist li.off{color:#536471}
#memberlist li.off .dot{background:#536471}
#memberlist .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;cursor:pointer}
#memberlist .st{margin-left:auto;font-size:12px;color:#8b98a5;flex:none}
#sidebtn{display:none;background:transparent;border:1px solid #2f3336;border-radius:16px;padding:6px 10px;font-size:14px;cursor:pointer;color:#e7e9ea}
#imgbtn{background:transparent;border:1px solid #2f3336;border-radius:20px;padding:10px 12px;font-size:16px;cursor:pointer;flex:none;line-height:1}
#imgbtn:disabled{opacity:.4;cursor:default}
#replybar{display:none;background:#15202b;border-top:1px solid #2f3336;padding:8px 12px;font-size:13px;color:#8b98a5}
#replybar.show{display:block}
#replyinner{display:flex;align-items:center;gap:8px;width:100%;max-width:1024px;margin:0 auto}
#replybar .rq{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-left:3px solid #1d9bf0;padding-left:8px}
#replybar .rx{cursor:pointer;padding:4px 10px;font-size:14px;flex:none}
.quote{border-left:3px solid #1d9bf0;background:rgba(29,155,240,.08);border-radius:6px;padding:6px 8px;margin-bottom:6px;font-size:13px;color:#8b98a5;cursor:pointer}
.quote .qn{color:#1d9bf0;font-weight:600;margin-bottom:2px}
.msg .meta{display:flex;align-items:center}
.repbtn{margin-left:8px;color:#1d9bf0;cursor:pointer;font-size:12px;opacity:.65;flex:none}
.repbtn:hover{opacity:1}
.msg.flash .bubble{animation:flash 1.2s}
@keyframes flash{0%,100%{box-shadow:none}30%{box-shadow:0 0 0 3px #1d9bf0}}
@media (max-width:768px){
#side{position:fixed;top:0;right:0;bottom:0;z-index:20;transform:translateX(105%);transition:transform .2s;box-shadow:-4px 0 12px rgba(0,0,0,.4)}
body.show-side #side{transform:none}
#sidebtn{display:inline-block}
header{padding:8px 10px;gap:8px}
header .room{font-size:15px}
#users{display:none}
#msgs{padding:10px;gap:8px}
.msg{max-width:92%}
.msg .bubble{font-size:16px;padding:7px 11px}
.msg .meta{font-size:12px}
#bar{padding:8px}
#barinner{gap:6px}
#input{font-size:15px;padding:9px 12px}
#bar button{padding:9px 16px;font-size:14px;flex:none}
}
#input{flex:1;background:#0f1419;border:1px solid #2f3336;border-radius:20px;color:#e7e9ea;padding:10px 14px;font-size:15px;outline:none}
#input:focus{border-color:#1d9bf0}
button{background:#1d9bf0;border:none;border-radius:20px;color:#fff;padding:10px 20px;font-size:15px;cursor:pointer}
button:active{opacity:.8}
.mention{color:#1d9bf0;font-weight:700;background:rgba(29,155,240,.12);border-radius:4px;padding:0 3px}
.mention-me{color:#fff;font-weight:700;background:#e0245e;border-radius:4px;padding:0 3px}
.mention-all{color:#fff;font-weight:700;background:#f59e0b;border-radius:4px;padding:0 3px}
#atbadge{display:none;color:#f4212e;font-size:13px;font-weight:700;cursor:pointer}
#memberlist .nm{cursor:pointer}
</style>
</head>
<body>
<header>
<div id="headinner">
<span class="room">💬 <span id="roomname"></span></span>
<span class="online" id="online"></span>
<span id="users"></span>
<span id="atbadge" onclick="atClear()" title="点我消掉提醒">🔴 有人@你</span>
<a href="/admin" title="分身管理" style="text-decoration:none;font-size:14px">⚙️</a>
<button id="sidebtn" onclick="document.body.classList.toggle('show-side')">👥</button>
</div>
</header>
<div id="wrap">
<div id="msgs"></div>
<aside id="side">
<h3>👥 成员 <span id="mcount"></span><span id="syncbtn" onclick="syncMenu()" title="换设备同步昵称和备注" style="cursor:pointer;font-size:14px;margin-left:6px">🔄</span></h3>
<ul id="memberlist"></ul>
</aside>
</div>
<div id="replybar"><div id="replyinner"><div class="rq" id="replyquote"></div><span class="rx" onclick="cancelReply()">✕</span></div></div>
<div id="imgbar" style="display:none"><div id="imginner" style="max-width:1024px;margin:0 auto;padding:6px 12px;display:flex;align-items:center;gap:8px"><img id="imgpreview" style="max-height:60px;border-radius:8px"><span style="font-size:13px;color:#8b98a5">图片已选，打字后按发送一起发出</span><span class="rx" onclick="cancelImage()" style="cursor:pointer">✕</span></div></div>
<div id="bar">
<div id="barinner">
<button id="imgbtn" onclick="document.getElementById('filepick').click()" title="发图片（输入框里也可直接 Ctrl+V 粘贴）">📷</button>
<input type="file" id="filepick" accept="image/*" style="display:none">
<input id="input" placeholder="说点什么…（@昵称 可点名）" autocomplete="off" maxlength="2000" onclick="atClear()">
<button onclick="send()">发送</button>
</div>
</div>
<script>
const qs = new URLSearchParams(location.search);
const ROOM = (qs.get('room') || 'lobby').slice(0, 32);
document.getElementById('roomname').textContent = ROOM;
let nick = localStorage.getItem('muse_chat_nick') || '';
while (!nick) {
  nick = (prompt('给自己起个昵称（字母/数字/中文，20字内）：') || '').trim();
}
localStorage.setItem('muse_chat_nick', nick);
// 房间密码：输入一次后记在本机
let roomPass = '';
try { roomPass = localStorage.getItem('muse_chat_pass_' + ROOM) || ''; } catch(e){}
async function ensurePass(){
  try {
    const r = await fetch('/api/needpass?room=' + encodeURIComponent(ROOM));
    const j = await r.json();
    if (!j.protected) return true;
  } catch(e){ return true; }
  while (true){
    if (!roomPass) roomPass = (prompt('这个房间需要密码：') || '').trim();
    if (!roomPass) return false;
    try {
      const t = await fetch('/api/messages?room=' + encodeURIComponent(ROOM) + '&since=0&nick=' + encodeURIComponent(nick) + '&pass=' + encodeURIComponent(roomPass));
      if (t.status !== 403){ try { localStorage.setItem('muse_chat_pass_' + ROOM, roomPass); } catch(e){} return true; }
    } catch(e){ return true; }
    alert('密码不对，再试一次');
    roomPass = '';
    try { localStorage.removeItem('muse_chat_pass_' + ROOM); } catch(e){}
  }
}
// 备注：只存本机，显示用，不影响 @ 和实际昵称
let remarks = {};
try { remarks = JSON.parse(localStorage.getItem('muse_chat_remarks') || '{}'); } catch(e){ remarks = {}; }
function saveRemarks(){ try { localStorage.setItem('muse_chat_remarks', JSON.stringify(remarks)); } catch(e){} }
function displayNick(n){ return remarks[n] ? (remarks[n] + ' (' + n + ')') : n; }
let lastMembers = [];
let lastId = 0;
let firstPoll = true;
const box = document.getElementById('msgs');
function esc(s){ return s; } // 用 textContent 渲染，无需转义
// @全体 / @all：at 所有人
function isAllMention(name){ return name === '全体' || name.toLowerCase() === 'all'; }
// @昵称 高亮：安全构建 DOM（createTextNode 转义），不注入 HTML
function richText(text, myNick){
  const frag = document.createDocumentFragment();
  const re = /[@＠]([\w\-\u4e00-\u9fff]{1,20})/g;
  let last = 0, mt;
  while ((mt = re.exec(text)) !== null){
    if (mt.index > last) frag.appendChild(document.createTextNode(text.slice(last, mt.index)));
    const sp = document.createElement('span');
    sp.className = (mt[1] === myNick) ? 'mention-me' : (isAllMention(mt[1]) ? 'mention-all' : 'mention');
    sp.textContent = '@' + mt[1];
    frag.appendChild(sp);
    last = mt.index + mt[0].length;
  }
  if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
  return frag;
}
function addMsg(m){
  if (document.getElementById('msg-' + m.id)) return; // 已显示过，不重复渲染
  const d = document.createElement('div');
  d.id = 'msg-' + m.id;
  if (m.nick === '系统') { d.className = 'msg sys'; }
  else { d.className = 'msg' + (m.nick === nick ? ' me' : ''); }
  const meta = document.createElement('div'); meta.className = 'meta';
  const t = new Date(m.ts * 1000);
  const mt = document.createElement('span');
  mt.textContent = displayNick(m.nick) + ' · ' + t.toTimeString().slice(0,5);
  meta.appendChild(mt);
  if (m.nick !== '系统'){
    const rb = document.createElement('span');
    rb.className = 'repbtn'; rb.textContent = '↩ 回复';
    rb.onclick = ev => { ev.stopPropagation(); setReply(m.id); };
    meta.appendChild(rb);
  }
  const b = document.createElement('div'); b.className = 'bubble';
  if (m.quote) {
    const q = document.createElement('div'); q.className = 'quote';
    const qn = document.createElement('div'); qn.className = 'qn'; qn.textContent = displayNick(m.quote.nick);
    const qt = document.createElement('div');
    qt.textContent = (m.quote.text || (m.quote.img ? '[图片]' : '')).slice(0, 100);
    q.appendChild(qn); q.appendChild(qt);
    const targetId = m.reply_to;
    q.onclick = () => {
      const el = document.getElementById('msg-' + targetId);
      if (el){ el.scrollIntoView({behavior:'smooth', block:'center'}); el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); }
    };
    b.appendChild(q);
  }
  if (m.img) {
    const im = document.createElement('img');
    im.src = m.img; im.alt = '图片'; im.loading = 'lazy';
    im.style.cssText = 'max-width:100%;border-radius:8px;display:block;cursor:zoom-in';
    im.onclick = () => window.open(m.img, '_blank');
    b.appendChild(im);
  }
  if (m.text) {
    const tx = document.createElement('div'); tx.appendChild(richText(m.text, nick));
    if (m.img) tx.style.marginTop = '6px';
    b.appendChild(tx);
  }
  d.appendChild(meta); d.appendChild(b); box.appendChild(d);
  while (box.children.length > 300) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}
function mentionsMe(m){
  if (!m.text) return false;
  // 引用回复时，引用内容里的 @ 不触发提醒（只查回复自己的正文）
  let text = m.text;
  if (m.quote && m.quote.text) {
    // 把引用文本从正文中去掉，避免引用里的 @ 误触发
    text = text.split(m.quote.text).join('');
  }
  const re = /[@＠]([\w\-\u4e00-\u9fff]{1,20})/g;
  let mt;
  while ((mt = re.exec(text)) !== null){
    if (mt[1].toLowerCase() === nick.toLowerCase() || isAllMention(mt[1])) return true;
  }
  return false;
}
let atBlink = null;
function atPing(){
  document.getElementById('atbadge').style.display = 'inline';
  if (!atBlink) atBlink = setInterval(() => {
    document.title = (document.title === 'Muse 聊天室') ? '【有人@你】Muse 聊天室' : 'Muse 聊天室';
  }, 900);
}
function atClear(){
  document.getElementById('atbadge').style.display = 'none';
  if (atBlink){ clearInterval(atBlink); atBlink = null; }
  document.title = 'Muse 聊天室';
}
async function poll(){
  try {
    const r = await fetch('/api/messages?room=' + encodeURIComponent(ROOM) + '&since=' + lastId + '&nick=' + encodeURIComponent(nick) + '&pass=' + encodeURIComponent(roomPass));
    if (r.status === 403){ roomPass = ''; try { localStorage.removeItem('muse_chat_pass_' + ROOM); } catch(e){} await ensurePass(); setTimeout(poll, 2000); return; }
    const j = await r.json();
    (j.messages || []).forEach(m => { msgById[m.id] = m; addMsg(m); lastId = Math.max(lastId, m.id); if (!firstPoll && m.nick !== nick && mentionsMe(m)) atPing(); });
    const on = j.online || [];
    document.getElementById('online').textContent = '在线 ' + on.length;
    document.getElementById('users').textContent = on.join(' · ');
    lastMembers = j.members || [];
    renderMembers(lastMembers);
    firstPoll = false;
  } catch(e){}
  setTimeout(poll, 2000);
}
function ago(ts){
  const s = Math.floor(Date.now()/1000) - ts;
  if (s < 60) return '刚刚在线';
  if (s < 3600) return Math.floor(s/60) + '分钟前';
  if (s < 86400) return Math.floor(s/3600) + '小时前';
  return Math.floor(s/86400) + '天前';
}
function renderMembers(ms){
  const ul = document.getElementById('memberlist');
  ul.innerHTML = '';
  ms.forEach(m => {
    const li = document.createElement('li');
    if (!m.online) li.className = 'off';
    const dot = document.createElement('span'); dot.className = 'dot';
    const nm = document.createElement('span'); nm.className = 'nm'; nm.textContent = displayNick(m.nick);
    nm.title = '点击@' + m.nick;
    nm.onclick = () => { const inp = document.getElementById('input'); const tag = '@' + m.nick + ' '; if (inp.value.indexOf(tag) < 0) inp.value += tag; inp.focus(); };
    li.appendChild(dot); li.appendChild(nm);
    const note = document.createElement('span');
    note.textContent = '📝'; note.title = '备注';
    note.style.cssText = 'cursor:pointer;font-size:12px;opacity:.7;margin-left:2px;flex:none';
    note.onclick = ev => { ev.stopPropagation(); editRemark(m.nick); };
    li.appendChild(note);
    if (m.nick === nick){
      const ed = document.createElement('span');
      ed.textContent = '✏️'; ed.title = '改名';
      ed.style.cssText = 'cursor:pointer;font-size:12px;opacity:.7;margin-left:2px;flex:none';
      ed.onclick = ev => { ev.stopPropagation(); renameMe(); };
      li.appendChild(ed);
    }
    const st = document.createElement('span'); st.className = 'st';
    st.textContent = m.online ? '在线' : ago(m.last);
    li.appendChild(st);
    ul.appendChild(li);
  });
  document.getElementById('mcount').textContent = '（' + ms.length + '）';
}
function renameMe(){
  const nn = (prompt('改个昵称（字母/数字/中文/下划线/横线，20字内）：', nick) || '').trim();
  if (!nn || nn === nick) return;
  if (!/^[\w\-\u4e00-\u9fff]{1,20}$/.test(nn)){ alert('昵称不合法，换一个试试'); return; }
  nick = nn;
  try { localStorage.setItem('muse_chat_nick', nick); } catch(e){}
  location.reload();
}
function editRemark(target){
  const cur = remarks[target] || '';
  const nn = (prompt('给 ' + target + ' 加个备注（留空清除）：', cur) || '').trim();
  if (nn){
    if (!/^[\w\-\u4e00-\u9fff]{1,20}$/.test(nn)){ alert('备注不合法，换一个试试'); return; }
    remarks[target] = nn;
  } else {
    delete remarks[target];
  }
  saveRemarks();
  refreshNickDisplay();
}
function refreshNickDisplay(){
  if (lastMembers.length) renderMembers(lastMembers);
  document.querySelectorAll('#msgs .msg').forEach(d => {
    const m = msgById[d.id.replace('msg-', '')];
    if (!m) return;
    const sp = d.querySelector('.meta span');
    if (!sp) return;
    const t = new Date(m.ts * 1000);
    sp.textContent = displayNick(m.nick) + ' · ' + t.toTimeString().slice(0,5);
  });
}
// 换设备同步：老设备上传昵称+备注生成同步码，新设备输入码下载
async function syncUpload(){
  try {
    const r = await fetch('/api/sync/upload', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({nick: nick, remarks: remarks})});
    const j = await r.json();
    if (j.ok) prompt('同步码（长期有效，新设备点 🔄 输入）：', j.code);
    else alert('上传失败');
  } catch(e){ alert('上传失败，检查网络'); }
}
async function syncDownload(){
  const code = (prompt('输入老设备上的同步码：') || '').trim();
  if (!code) return;
  try {
    const r = await fetch('/api/sync/download?code=' + encodeURIComponent(code));
    const j = await r.json();
    if (!j.ok){ alert(j.error || '同步码无效'); return; }
    const d = j.data || {};
    if (d.nick && /^[\w\-\u4e00-\u9fff]{1,20}$/.test(d.nick)){
      nick = d.nick;
      try { localStorage.setItem('muse_chat_nick', nick); } catch(e){}
    }
    if (d.remarks && typeof d.remarks === 'object'){
      remarks = {};
      for (const k of Object.keys(d.remarks)){
        const v = String(d.remarks[k]);
        if (/^[\w\-\u4e00-\u9fff]{1,20}$/.test(k) && /^[\w\-\u4e00-\u9fff]{1,20}$/.test(v)) remarks[k] = v;
      }
      saveRemarks();
    }
    alert('同步成功，页面刷新生效');
    location.reload();
  } catch(e){ alert('下载失败，检查网络'); }
}
function syncMenu(){
  if (confirm('🔄 同步设置\\n\\n[确定] 我是老设备：上传昵称+备注，生成同步码\\n[取消] 我是新设备：输入同步码下载')) syncUpload();
  else syncDownload();
}
let replyTo = 0;
const msgById = {};
function setReply(id){
  const m = msgById[id];
  if (!m) return;
  replyTo = id;
  document.getElementById('replyquote').textContent =
    '回复 ' + displayNick(m.nick) + '：' + ((m.text || (m.img ? '[图片]' : '')).slice(0, 60));
  document.getElementById('replybar').classList.add('show');
  document.getElementById('input').focus();
}
function cancelReply(){
  replyTo = 0;
  document.getElementById('replybar').classList.remove('show');
}
async function send(){
  const inp = document.getElementById('input');
  const text = inp.value.trim();
  if (!text && !pendingImage) return;
  inp.value = '';
  const btn = document.getElementById('imgbtn');
  btn.disabled = true;
  try {
    let imgUrl = '';
    if (pendingImage){
      try { imgUrl = await uploadPending(); }
      catch(err){ alert('图片发送失败：' + err.message); btn.disabled = false; return; }
      cancelImage();
    }
    const body = {room: ROOM, nick: nick, text: text, pass: roomPass};
    if (imgUrl) body.img = imgUrl;
    if (replyTo) body.reply_to = replyTo;
    cancelReply();
    await fetch('/api/send', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)});
  } catch(e){}
  finally { btn.disabled = false; }
  pollSoon();
}
let pollTimer = null;
function pollSoon(){ clearTimeout(pollTimer); pollTimer = setTimeout(poll, 300); }
document.getElementById('input').addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
// 待发送的图片：选图/粘贴后先预览，打字按发送才一起发出
let pendingImage = null;
function selectImage(f){
  if (!f) return;
  if (f.size > 10*1024*1024) { alert('图片太大，最大 10MB'); return; }
  pendingImage = f;
  const url = URL.createObjectURL(f);
  document.getElementById('imgpreview').src = url;
  document.getElementById('imgbar').style.display = 'block';
  document.getElementById('input').focus();
}
function cancelImage(){
  pendingImage = null;
  document.getElementById('imgbar').style.display = 'none';
  document.getElementById('imgpreview').src = '';
}
async function uploadPending(){
  if (!pendingImage) return '';
  const f = pendingImage;
  const r = await fetch('/api/upload?room=' + encodeURIComponent(ROOM) + '&pass=' + encodeURIComponent(roomPass), {method: 'POST',
    headers: {'Content-Type': f.type || 'application/octet-stream'}, body: f});
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || r.status);
  return j.url;
}
document.getElementById('filepick').addEventListener('change', e => {
  const f = e.target.files[0];
  e.target.value = '';
  selectImage(f);
});
document.addEventListener('paste', e => {
  const items = (e.clipboardData && e.clipboardData.items) || [];
  for (const it of items) {
    if (it.type && it.type.indexOf('image/') === 0) {
      e.preventDefault();
      selectImage(it.getAsFile());
      return;
    }
  }
  // 纯文本粘贴走默认行为（输入框正常粘贴）
});
(async () => { if (await ensurePass()) poll(); })();
</script>
</body>
</html>
"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Muse 分身管理</title>
<style>
body{background:#000;color:#e7e9ea;font-family:system-ui;margin:0;padding:20px}
.wrap{max-width:800px;margin:0 auto}
h1{font-size:20px}
.card{background:#16181c;border:1px solid #2f3336;border-radius:12px;padding:16px;margin:12px 0}
.card .nick{font-size:16px;font-weight:700;margin-bottom:8px}
.card textarea{width:100%;min-height:120px;background:#0a0a0a;color:#e7e9ea;border:1px solid #2f3336;border-radius:8px;padding:10px;font-size:14px;box-sizing:border-box}
.card .row{margin-top:10px;display:flex;gap:8px}
button{background:#1d9bf0;color:#fff;border:none;border-radius:20px;padding:8px 20px;font-size:14px;cursor:pointer}
button.del{background:#e0245e}
button.ghost{background:transparent;border:1px solid #2f3336;color:#e7e9ea}
input.nickinput{background:#0a0a0a;color:#e7e9ea;border:1px solid #2f3336;border-radius:8px;padding:8px;font-size:14px;width:200px}
.hint{color:#8b98a5;font-size:13px;margin:8px 0}
a{color:#1d9bf0}
</style>
</head>
<body>
<div class="wrap">
<h1>🤖 Muse 分身管理</h1>
<div class="hint">在这里改每个分身的人设，保存后立刻生效（分身下次被 @ 时按新人设回复）。<a href="/">← 回聊天室</a></div>
<div class="card">
<div class="nick">🔒 房间密码</div>
<div class="hint">给 lobby 设密码后，进房间要先输密码。留空=不设密码。</div>
<div class="row"><input class="nickinput" id="roompass" type="password" placeholder="新密码（留空清除）" style="width:240px">
<button onclick="setPass()">设置密码</button></div>
<div class="hint" id="passstatus"></div>
</div>
<div id="list"></div>
<div class="card">
<div class="nick">＋ 添加新分身</div>
<input class="nickinput" id="newnick" placeholder="昵称（字母/数字/中文）">
<div style="margin-top:8px"><textarea id="newpersona" placeholder="人设描述，比如：毒舌程序员，说话一针见血…"></textarea></div>
<div class="row"><button onclick="addNew()">添加</button></div>
</div>
</div>
<script>
let personas = {};
async function load(){
  const r = await fetch('/api/personas');
  const j = await r.json();
  personas = j.personas || {};
  render();
  try {
    const pr = await fetch('/api/needpass?room=lobby');
    const pj = await pr.json();
    document.getElementById('passstatus').textContent = pj.protected ? '当前状态：已设密码' : '当前状态：未设密码';
  } catch(e){}
}
async function setPass(){
  const np = document.getElementById('roompass').value;
  let old = '';
  try {
    const pr = await fetch('/api/needpass?room=lobby');
    if ((await pr.json()).protected) old = prompt('请输入原密码：') || '';
  } catch(e){}
  const r = await fetch('/api/setpass', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({room: 'lobby', password: np, old: old})});
  const j = await r.json();
  if (j.ok){ document.getElementById('passstatus').textContent = j.protected ? '当前状态：已设密码' : '当前状态：未设密码'; document.getElementById('roompass').value=''; alert('已保存'); }
  else alert('失败：' + (j.error || r.status));
}
function esc(s){ const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function render(){
  const list = document.getElementById('list');
  list.innerHTML = '';
  Object.keys(personas).forEach(nick => {
    const d = document.createElement('div'); d.className = 'card';
    const t = document.createElement('div'); t.className = 'nick'; t.textContent = nick;
    const ta = document.createElement('textarea'); ta.value = personas[nick]; ta.dataset.nick = nick;
    const row = document.createElement('div'); row.className = 'row';
    const b1 = document.createElement('button'); b1.textContent = '保存';
    b1.onclick = () => { personas[nick] = ta.value; saveAll(personas); };
    const b2 = document.createElement('button'); b2.textContent = '删除'; b2.className = 'del';
    b2.onclick = () => delOne(nick);
    row.appendChild(b1); row.appendChild(b2);
    d.appendChild(t); d.appendChild(ta); d.appendChild(row);
    list.appendChild(d);
  });
  if (!Object.keys(personas).length) list.innerHTML = '<div class="hint">还没有分身，先添加一个吧。</div>';
}
async function saveAll(p){
  const r = await fetch('/api/personas', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({personas: p})});
  const j = await r.json();
  if (j.ok){ personas = j.personas; render(); alert('已保存'); }
  else alert('保存失败');
}
function delOne(nick){
  if (!confirm('删除分身 ' + nick + '？')) return;
  delete personas[nick];
  saveAll(personas);
}
function addNew(){
  const n = document.getElementById('newnick').value.trim();
  const p = document.getElementById('newpersona').value.trim();
  if (!n){ alert('填个昵称'); return; }
  if (!/^[\\w\\-\\u4e00-\\u9fff]{1,20}$/.test(n)){ alert('昵称不合法'); return; }
  personas[n] = p;
  document.getElementById('newnick').value = '';
  document.getElementById('newpersona').value = '';
  saveAll(personas);
}
load();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "muse-chatroom/1.0"

    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/admin":
            body = ADMIN_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path.startswith("/uploads/"):
            fname = u.path[len("/uploads/"):]
            if not re.fullmatch(r"[0-9a-f]{32}\.(jpg|png|gif|webp)", fname):
                self._json({"error": "not found"}, 404)
                return
            p = os.path.join(UPLOAD_DIR, fname)
            if not os.path.isfile(p):
                self._json({"error": "not found"}, 404)
                return
            ctype = {"jpg": "image/jpeg", "png": "image/png",
                     "gif": "image/gif", "webp": "image/webp"}[fname.rsplit(".", 1)[1]]
            try:
                with open(p, "rb") as f:
                    body = f.read()
            except Exception:
                self._json({"error": "not found"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/messages":
            q = parse_qs(u.query)
            room_name = safe_room(q.get("room", ["lobby"])[0])
            if not _check_room_pass(room_name, q.get("pass", [""])[0]):
                self._json({"error": "密码不对"}, 403)
                return
            since = int(q.get("since", ["0"])[0] or 0)
            nick = safe_nick(q.get("nick", [""])[0])
            room = get_room(room_name)
            now = time.time()
            with _rooms_lock:
                if nick:
                    room["presence"][nick] = now
                    room["seen"][nick] = now
                prune_presence(room)
                msgs = [m for m in room["messages"] if m["id"] > since][-200:]
                by_id = {m["id"]: m for m in room["messages"]}
                out = []
                for m in msgs:
                    d = dict(m)
                    rt = m.get("reply_to")
                    if rt and rt in by_id:
                        q = by_id[rt]
                        d["quote"] = {"nick": q["nick"], "text": q["text"][:100],
                                      "img": bool(q.get("img"))}
                    out.append(d)
                online = sorted(room["presence"].keys())
                members = [{"nick": n, "online": n in room["presence"], "last": int(t)}
                           for n, t in sorted(room["seen"].items(),
                                              key=lambda kv: (kv[0] not in room["presence"], kv[0]))]
            self._json({"messages": out, "online": online, "members": members, "room": room_name})
            return
        if u.path == "/api/rooms":
            with _rooms_lock:
                names = sorted(_rooms.keys())
            try:
                files = [f[:-6] for f in os.listdir(DATA_DIR) if f.endswith(".jsonl")]
            except Exception:
                files = []
            self._json({"rooms": sorted(set(names) | set(files))})
            return
        if u.path == "/api/sync/download":
            q = parse_qs(u.query)
            code = (q.get("code", [""])[0] or "").strip()
            with _sync_lock:
                item = _sync_store.get(code)
                if not item:
                    self._json({"ok": False, "error": "同步码无效"}, 404)
                    return
                data = item["data"]
            self._json({"ok": True, "data": data})
            return
        if u.path == "/api/personas":
            self._json({"personas": _personas_load()})
            return
        if u.path == "/api/needpass":
            q = parse_qs(u.query)
            room_name = safe_room(q.get("room", ["lobby"])[0])
            pw = _passwords_load().get(room_name, "")
            self._json({"protected": bool(pw)})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/setpass":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"ok": False, "error": "请求无效"}, 400)
                return
            room_name = safe_room(str(data.get("room", "lobby")))
            newpw = str(data.get("password", ""))[:64]
            oldpw = str(data.get("old", ""))
            pwds = _passwords_load()
            cur = pwds.get(room_name, "")
            if cur and oldpw != cur:
                self._json({"ok": False, "error": "原密码不对"}, 403)
                return
            if newpw:
                pwds[room_name] = newpw
            else:
                pwds.pop(room_name, None)
            _passwords_save(pwds)
            self._json({"ok": True, "protected": bool(newpw)})
            return
        if u.path == "/api/personas":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"ok": False, "error": "请求无效"}, 400)
                return
            personas = data.get("personas") or {}
            clean = {}
            if isinstance(personas, dict):
                for k, v in list(personas.items())[:50]:
                    ks = str(k)[:20]
                    if re.fullmatch(r"[\w\-\u4e00-\u9fff]{1,20}", ks):
                        clean[ks] = str(v)[:2000]
            _personas_save(clean)
            self._json({"ok": True, "personas": clean})
            return
        if u.path == "/api/sync/upload":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"ok": False, "error": "请求无效"}, 400)
                return
            nick = safe_nick(str(data.get("nick", "")))
            remarks = data.get("remarks") or {}
            clean = {}
            if isinstance(remarks, dict):
                for k, v in list(remarks.items())[:200]:
                    ks, vs = str(k)[:20], str(v)[:20]
                    if re.fullmatch(r"[\w\-\u4e00-\u9fff]{1,20}", ks) and \
                       re.fullmatch(r"[\w\-\u4e00-\u9fff]{1,20}", vs):
                        clean[ks] = vs
            now = time.time()
            with _sync_lock:
                # 防刷：超上限删最早的（dict 保持插入顺序）
                while len(_sync_store) >= _SYNC_MAX:
                    _sync_store.pop(next(iter(_sync_store)))
                code = "%06d" % secrets.randbelow(1000000)
                while code in _sync_store:
                    code = "%06d" % secrets.randbelow(1000000)
                _sync_store[code] = {"data": {"nick": nick, "remarks": clean}}
                _sync_save()
            self._json({"ok": True, "code": code})
            return
        if u.path == "/api/upload":
            q = parse_qs(u.query)
            # 上传按 room 鉴权（query 传 room+pass，body 是 raw 图片）
            up_room = safe_room(q.get("room", ["lobby"])[0])
            if not _check_room_pass(up_room, q.get("pass", [""])[0]):
                self._json({"error": "密码不对"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
            except Exception:
                length = 0
            if length <= 0:
                self._json({"error": "空文件"}, 400)
                return
            if length > MAX_UPLOAD:
                self._json({"error": "图片太大，最大 10MB"}, 413)
                return
            ip = self.client_address[0]
            if not check_rate(ip):
                self._json({"error": "发得太快了，歇几秒"}, 429)
                return
            try:
                data = self.rfile.read(length)
            except Exception:
                self._json({"error": "读取失败"}, 400)
                return
            ext = img_ext(data)
            if not ext:
                self._json({"error": "只支持 JPG/PNG/GIF/WebP 图片"}, 400)
                return
            name = secrets.token_hex(16) + ext
            try:
                with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
                    f.write(data)
            except Exception:
                self._json({"error": "保存失败"}, 500)
                return
            self._json({"ok": True, "url": "/uploads/" + name})
            return
        if u.path != "/api/send":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except Exception:
            length = 0
        if length > 65536:
            self._json({"error": "body too large"}, 413)
            return
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json({"error": "bad json"}, 400)
            return
        room_name = safe_room(data.get("room"))
        if not _check_room_pass(room_name, str(data.get("pass", ""))):
            self._json({"error": "密码不对"}, 403)
            return
        nick = safe_nick(data.get("nick"))
        text = str(data.get("text", ""))[:MAX_TEXT].strip()
        img = str(data.get("img", ""))[:64]
        try:
            reply_to = int(data.get("reply_to") or 0)
        except (TypeError, ValueError):
            reply_to = 0
        if img:
            if not re.fullmatch(r"/uploads/[0-9a-f]{32}\.(jpg|png|gif|webp)", img):
                self._json({"error": "图片地址不合法"}, 400)
                return
            if not os.path.isfile(os.path.join(UPLOAD_DIR, img.rsplit("/", 1)[1])):
                self._json({"error": "图片不存在"}, 400)
                return
        if not nick:
            self._json({"error": "昵称不合法（字母/数字/中文/下划线/横线，1-20字）"}, 400)
            return
        if not text and not img:
            self._json({"error": "消息为空"}, 400)
            return
        ip = self.client_address[0]
        if not check_rate(ip):
            self._json({"error": "发得太快了，歇几秒"}, 429)
            return
        room = get_room(room_name)
        with _rooms_lock:
            room["seq"] += 1
            msg = {"id": room["seq"], "nick": nick, "text": text, "ts": int(time.time())}
            if img:
                msg["img"] = img
            if reply_to > 0 and any(m["id"] == reply_to for m in room["messages"]):
                msg["reply_to"] = reply_to
            room["messages"].append(msg)
            room["messages"] = room["messages"][-MAX_KEEP:]
            room["presence"][nick] = time.time()
            room["seen"][nick] = msg["ts"]
        persist(room_name, msg)
        self._json({"ok": True, "id": msg["id"]})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"muse-chatroom listening on 0.0.0.0:{PORT}, data={DATA_DIR}", flush=True)
    srv.serve_forever()
