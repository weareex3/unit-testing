const path = require('path');
const express = require('express');
const QRCode = require('qrcode');
const { Server } = require('socket.io');
const http = require('http');
const { SYMBOLS, DECK } = require('./lib/deck');

const app = express();
const server = http.createServer(app);
const io = new Server(server);

const PORT = process.env.PORT || 3000;
const CODE_CHARS = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'; // no 0/O/1/I/L
const ROOM_TTL_MS = 6 * 60 * 60 * 1000;

/** @type {Map<string, any>} */
const rooms = new Map();

function makeRoomCode() {
  let code;
  do {
    code = Array.from({ length: 4 }, () => CODE_CHARS[Math.floor(Math.random() * CODE_CHARS.length)]).join('');
  } while (rooms.has(code));
  return code;
}

function shuffledIndices(n) {
  const arr = Array.from({ length: n }, (_, i) => i);
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

function commonSymbol(cardA, cardB) {
  const setA = new Set(cardA);
  return cardB.find((id) => setA.has(id));
}

function scoreboard(room) {
  return Array.from(room.players.values())
    .map((p) => ({ name: p.name, score: p.score }))
    .sort((a, b) => b.score - a.score);
}

function buildCard(indices) {
  return indices.map((id) => ({ id, emoji: SYMBOLS[id].emoji, label: SYMBOLS[id].label }));
}

function startRound(code) {
  const room = rooms.get(code);
  if (!room) return;

  if (room.roundNumber >= room.totalRounds || room.cardOrder.length < 2) {
    room.started = false;
    io.to(code).emit('game:over', { scores: scoreboard(room) });
    return;
  }

  const aIdx = room.cardOrder.pop();
  const bIdx = room.cardOrder.pop();
  const cardA = DECK[aIdx];
  const cardB = DECK[bIdx];
  const commonId = commonSymbol(cardA, cardB);

  room.roundNumber += 1;
  room.roundActive = true;
  room.currentCommonId = commonId;

  io.to(code).emit('round:new', {
    roundNumber: room.roundNumber,
    totalRounds: room.totalRounds,
    cardA: buildCard(cardA),
    cardB: buildCard(cardB),
  });

  clearTimeout(room.roundTimer);
  room.roundTimer = setTimeout(() => {
    if (!room.roundActive) return;
    room.roundActive = false;
    io.to(code).emit('round:timeout', {
      symbolId: commonId,
      emoji: SYMBOLS[commonId].emoji,
      label: SYMBOLS[commonId].label,
    });
    setTimeout(() => startRound(code), 3000);
  }, 20000);
}

io.on('connection', (socket) => {
  socket.on('host:create', (payload, ack) => {
    const rounds = Math.max(3, Math.min(28, Number(payload && payload.rounds) || 15));
    const code = makeRoomCode();
    rooms.set(code, {
      code,
      hostSocketId: socket.id,
      players: new Map(),
      started: false,
      totalRounds: rounds,
      roundNumber: 0,
      cardOrder: [],
      roundActive: false,
      currentCommonId: null,
      roundTimer: null,
      createdAt: Date.now(),
    });
    socket.join(code);
    socket.data.role = 'host';
    socket.data.code = code;
    if (typeof ack === 'function') ack({ code });
  });

  socket.on('player:join', (payload, ack) => {
    const code = String((payload && payload.code) || '').toUpperCase();
    const name = String((payload && payload.name) || '').trim().slice(0, 20) || 'Player';
    const room = rooms.get(code);
    if (!room) {
      if (typeof ack === 'function') ack({ ok: false, error: 'Room not found' });
      return;
    }
    room.players.set(socket.id, { name, score: 0 });
    socket.join(code);
    socket.data.role = 'player';
    socket.data.code = code;
    if (typeof ack === 'function') ack({ ok: true, code, name, started: room.started });
    io.to(code).emit('players:update', scoreboard(room));
  });

  socket.on('host:start', (payload) => {
    const code = String((payload && payload.code) || '').toUpperCase();
    const room = rooms.get(code);
    if (!room || room.hostSocketId !== socket.id || room.players.size < 1) return;
    room.started = true;
    room.roundNumber = 0;
    room.cardOrder = shuffledIndices(DECK.length);
    for (const p of room.players.values()) p.score = 0;
    startRound(code);
  });

  socket.on('host:playAgain', (payload) => {
    const code = String((payload && payload.code) || '').toUpperCase();
    const room = rooms.get(code);
    if (!room || room.hostSocketId !== socket.id) return;
    room.started = true;
    room.roundNumber = 0;
    room.cardOrder = shuffledIndices(DECK.length);
    for (const p of room.players.values()) p.score = 0;
    io.to(code).emit('players:update', scoreboard(room));
    startRound(code);
  });

  socket.on('player:answer', (payload) => {
    const code = String((payload && payload.code) || '').toUpperCase();
    const symbolId = Number(payload && payload.symbolId);
    const room = rooms.get(code);
    if (!room || !room.roundActive) return;
    if (symbolId !== room.currentCommonId) return;

    const player = room.players.get(socket.id);
    if (!player) return;

    room.roundActive = false;
    clearTimeout(room.roundTimer);
    player.score += 1;

    io.to(code).emit('round:result', {
      winnerName: player.name,
      symbolId,
      emoji: SYMBOLS[symbolId].emoji,
      label: SYMBOLS[symbolId].label,
      scores: scoreboard(room),
    });

    setTimeout(() => startRound(code), 3000);
  });

  socket.on('disconnect', () => {
    const code = socket.data.code;
    if (!code) return;
    const room = rooms.get(code);
    if (!room) return;
    if (socket.data.role === 'player' && room.players.delete(socket.id)) {
      io.to(code).emit('players:update', scoreboard(room));
    }
  });
});

// Periodic cleanup of stale rooms.
setInterval(() => {
  const now = Date.now();
  for (const [code, room] of rooms) {
    if (now - room.createdAt > ROOM_TTL_MS) rooms.delete(code);
  }
}, 30 * 60 * 1000);

app.get('/api/qr', async (req, res) => {
  const code = String(req.query.code || '').toUpperCase();
  if (!rooms.has(code)) return res.status(404).json({ error: 'Room not found' });
  const joinUrl = `${req.protocol}://${req.get('host')}/play/${code}`;
  try {
    const qrDataUrl = await QRCode.toDataURL(joinUrl, { margin: 1, width: 320 });
    res.json({ joinUrl, qrDataUrl });
  } catch (err) {
    res.status(500).json({ error: 'QR generation failed' });
  }
});

app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.get('/play/:code', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'play.html'));
});

app.use(express.static(path.join(__dirname, 'public')));

server.listen(PORT, () => {
  console.log(`Dobble Party listening on port ${PORT}`);
});
