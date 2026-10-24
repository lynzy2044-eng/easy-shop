const socket = io({ transports: ['websocket'] });
const listEl = document.getElementById('adm-list');
const boxEl = document.getElementById('adm-box');
const formEl = document.getElementById('adm-form');
const inputEl = document.getElementById('adm-input');
const typingEl = document.getElementById('adm-typing');
const nameEl = document.getElementById('adm-name');

const users = new Map();          // user_id -> {id, username, unread, last}
let currentId = null;
let typingTimer = null, lastTypingSent = 0;

function makeRow(m) {
  const row = document.createElement('div');
  row.className = 'adm-row ' + (m.sender === 'admin' ? 'admin' : 'user');
  const bubble = document.createElement('div');
  bubble.className = 'adm-bubble';
  if (m.body.startsWith('data:image/')) {
    const img = document.createElement('img');
    img.src = m.body;
    img.style.maxWidth = '100%';
    img.style.borderRadius = '8px';
    bubble.appendChild(img);
  } else {
    bubble.textContent = m.body;
  }
  const t = document.createElement('span');
  t.className = 'adm-time';
  t.textContent = m.time || '';
  bubble.appendChild(t);
  row.appendChild(bubble);
  return row;
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
    name.textContent = '👤 ' + u.username.split('@')[0];
    el.appendChild(name);
    if (u.unread > 0) {
      const b = document.createElement('span');
      b.className = 'adm-badge';
      b.textContent = u.unread;
      el.appendChild(b);
    }
    el.onclick = () => openChat(u.id, u.username.split('@')[0]);
    listEl.appendChild(el);
  });
}

function openChat(id, name) {
  currentId = id;
  nameEl.textContent = 'คุยกับ: ' + name;
  const u = users.get(id);
  if (u) u.unread = 0;
  drawList();
  socket.emit('load_history', { user_id: id }, msgs => {
    boxEl.innerHTML = '';
    msgs.forEach(m => boxEl.appendChild(makeRow(m)));
    boxEl.scrollTop = boxEl.scrollHeight;
  });
  socket.emit('mark_read', { user_id: id });
}

// โหลดรายชื่อครั้งแรก และเมื่อ reconnect
socket.on('connect', () => {
  socket.emit('list_chats', {}, list => {
    users.clear();
    list.forEach(u => users.set(u.id, { ...u, last: new Date(u.last).getTime() }));
    drawList();
  });
});

socket.on('new_message', m => {
  if (m.sender === 'user') {
    const u = users.get(m.user_id) || { id: m.user_id, username: m.username, unread: 0 };
    u.username = m.username;
    u.last = Date.now();
    if (m.user_id === currentId) {
      socket.emit('mark_read', { user_id: currentId });
    } else {
      u.unread += 1;
    }
    users.set(m.user_id, u);
    drawList();
  }
  if (m.user_id === currentId) {
    boxEl.appendChild(makeRow(m));
    boxEl.scrollTop = boxEl.scrollHeight;
    if (m.sender === 'user') typingEl.style.display = 'none';
  }
});

socket.on('typing', d => {
  if (d.user_id !== currentId) return;
  typingEl.style.display = 'block';
  clearTimeout(typingTimer);
  typingTimer = setTimeout(() => typingEl.style.display = 'none', 2500);
});

inputEl.addEventListener('input', () => {
  if (!currentId) return;
  const now = Date.now();
  if (now - lastTypingSent > 1500) {
    socket.emit('typing', { user_id: currentId });
    lastTypingSent = now;
  }
});

formEl.addEventListener('submit', e => {
  e.preventDefault();
  const text = inputEl.value.trim();
  if (!text || !currentId) return;
  socket.emit('send_message', { body: text, target_id: currentId });
  inputEl.value = '';
});
