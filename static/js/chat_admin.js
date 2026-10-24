const CONF = window.CHAT_CONF;
const pusher = new Pusher(CONF.key, {
  cluster: CONF.cluster,
  channelAuthorization: { endpoint: CONF.authEndpoint }
});

const listEl = document.getElementById('adm-list');
const boxEl = document.getElementById('adm-box');
const formEl = document.getElementById('adm-form');
const inputEl = document.getElementById('adm-input');
const typingEl = document.getElementById('adm-typing');
const nameEl = document.getElementById('adm-name');

const users = new Map();      // id -> {id, username, unread, last}
let currentId = null;
let userChannel = null;
let seen = new Set();
let typingTimer = null, lastTypingSent = 0;

const adminChannel = pusher.subscribe('private-admins');

function displayName(full) {
  return (full || '').split('@')[0];
}

function makeRow(m) {
  const row = document.createElement('div');
  row.className = 'adm-row ' + (m.sender === 'admin' ? 'admin' : 'user');
  const bubble = document.createElement('div');
  bubble.className = 'adm-bubble';
  if (m.body.startsWith('/api/chat/image/')) {
    const img = document.createElement('img');
    img.src = m.body;
    img.style.maxWidth = '100%';
    img.style.borderRadius = '8px';
    img.onclick = () => window.open(img.src);
    bubble.appendChild(img);
  } else {
    bubble.textContent = m.body;          // ไม่ใช้ innerHTML กัน XSS
  }
  const t = document.createElement('span');
  t.className = 'adm-time';
  t.textContent = m.time || '';
  bubble.appendChild(t);
  row.appendChild(bubble);
  return row;
}

function appendMsg(m) {
  if (seen.has(m.id)) return;             // กันข้อความซ้ำ
  seen.add(m.id);
  boxEl.appendChild(makeRow(m));
  boxEl.scrollTop = boxEl.scrollHeight;
}

function drawList() {
  listEl.innerHTML = '';
  if (!users.size) {
    listEl.innerHTML = '<div class="adm-empty">ยังไม่มีใครทักมา</div>';
    return;
  }
  [...users.values()].sort((a, b) => b.last - a.last).forEach(u => {
    const el = document.createElement('div');
    el.className = 'adm-item' + (u.id === currentId ? ' active' : '');
    const name = document.createElement('span');
    name.textContent = '👤 ' + displayName(u.username);
    el.appendChild(name);
    if (u.unread > 0) {
      const b = document.createElement('span');
      b.className = 'adm-badge';
      b.textContent = u.unread;
      el.appendChild(b);
    }
    el.onclick = () => openChat(u.id, u.username);
    listEl.appendChild(el);
  });
}

function showTyping() {
  typingEl.style.display = 'block';
  clearTimeout(typingTimer);
  typingTimer = setTimeout(() => typingEl.style.display = 'none', 2500);
}

async function openChat(id, username) {
  currentId = id;
  seen = new Set();
  boxEl.innerHTML = '';
  nameEl.textContent = 'คุยกับ: ' + displayName(username);

  // ย้ายไปฟังช่องของผู้ใช้คนนี้ (ใช้รับและส่งสถานะกำลังพิมพ์)
  if (userChannel) pusher.unsubscribe(userChannel.name);
  userChannel = pusher.subscribe(`private-user-${id}`);
  userChannel.bind('client-typing', showTyping);

  const u = users.get(id);
  if (u) u.unread = 0;
  drawList();

  const res = await fetch(`${CONF.messagesEndpoint}?user_id=${id}`);
  const data = await res.json();
  data.messages.forEach(appendMsg);
  boxEl.scrollTop = boxEl.scrollHeight;
}

async function loadUsers() {
  try {
    const res = await fetch(CONF.usersEndpoint);
    const data = await res.json();
    users.clear();
    data.users.forEach(u => users.set(u.id, {
      id: u.id,
      username: u.username,
      unread: u.unread,
      last: u.last ? new Date(u.last).getTime() : 0,
    }));
    drawList();
  } catch (e) { console.error(e); }
}

// ข้อความใหม่จากผู้ใช้ทุกคน หรือที่แอดมินส่ง
adminChannel.bind('new_message', m => {
  const entry = users.get(m.user_id) || { id: m.user_id, username: m.username, unread: 0, last: 0 };
  entry.username = m.username;
  entry.last = Date.now();
  if (m.sender === 'user' && m.user_id !== currentId) entry.unread += 1;
  users.set(m.user_id, entry);
  drawList();

  if (m.user_id === currentId) {
    appendMsg(m);
    if (m.sender === 'user') {
      typingEl.style.display = 'none';
      fetch(CONF.readEndpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: m.user_id })
      });
    }
  }
});

// แอดมินพิมพ์ → แจ้งผู้ใช้ผ่านช่องของผู้ใช้คนนั้น
inputEl.addEventListener('input', () => {
  if (!userChannel || !userChannel.subscribed) return;
  const now = Date.now();
  if (now - lastTypingSent > 1500) {
    userChannel.trigger('client-typing', {});
    lastTypingSent = now;
  }
});

formEl.addEventListener('submit', async e => {
  e.preventDefault();
  const text = inputEl.value.trim();
  if (!text || !currentId) return;
  inputEl.value = '';
  const res = await fetch(CONF.sendEndpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: text, target_id: currentId })
  });
  if (!res.ok) alert('ส่งข้อความไม่สำเร็จ');
});

loadUsers();
