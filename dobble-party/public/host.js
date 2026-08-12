const socket = io();
let roomCode = null;

const el = (id) => document.getElementById(id);
const setup = el('setup');
const lobby = el('lobby');
const gameArea = el('gameArea');
const gameOver = el('gameOver');
const resultOverlay = el('resultOverlay');

el('createBtn').addEventListener('click', () => {
  const rounds = parseInt(el('roundsInput').value, 10) || 15;
  socket.emit('host:create', { rounds }, (res) => {
    roomCode = res.code;
    el('roomCode').textContent = roomCode;
    setup.classList.add('hidden');
    lobby.classList.remove('hidden');
    fetch(`/api/qr?code=${roomCode}`)
      .then((r) => r.json())
      .then((data) => {
        el('qrImg').src = data.qrDataUrl;
        el('joinUrlText').textContent = data.joinUrl;
      });
  });
});

el('startBtn').addEventListener('click', () => {
  socket.emit('host:start', { code: roomCode });
  lobby.classList.add('hidden');
  gameArea.classList.remove('hidden');
});

el('playAgainBtn').addEventListener('click', () => {
  socket.emit('host:playAgain', { code: roomCode });
  gameOver.classList.add('hidden');
  gameArea.classList.remove('hidden');
});

socket.on('players:update', (scores) => {
  el('playerCount').textContent = scores.length;
  el('startBtn').disabled = scores.length < 1;
  el('playerList').innerHTML = scores.map((p) => `<li>${p.name}</li>`).join('');
  renderLeaderboard('leaderboard', scores);
});

socket.on('round:new', (data) => {
  el('roundNum').textContent = data.roundNumber;
  el('totalRounds').textContent = data.totalRounds;
  renderCard(el('cardA'), data.cardA);
  renderCard(el('cardB'), data.cardB);
});

socket.on('round:result', (data) => {
  showOverlay('🎉', `${data.winnerName} found it!`, `${data.emoji} ${data.label}`);
  renderLeaderboard('leaderboard', data.scores);
});

socket.on('round:timeout', (data) => {
  showOverlay('⏰', "Time's up!", `It was ${data.emoji} ${data.label}`);
});

socket.on('game:over', (data) => {
  gameArea.classList.add('hidden');
  gameOver.classList.remove('hidden');
  renderLeaderboard('finalLeaderboard', data.scores);
});

function showOverlay(emoji, title, label) {
  el('resultEmoji').textContent = emoji;
  el('resultTitle').textContent = title;
  el('resultLabel').textContent = label;
  resultOverlay.classList.remove('hidden');
  setTimeout(() => resultOverlay.classList.add('hidden'), 2400);
}

function renderLeaderboard(targetId, scores) {
  const medals = ['🥇', '🥈', '🥉'];
  el(targetId).innerHTML = scores
    .map((p, i) => `<div class="leaderboard-row"><span>${medals[i] || ''} ${p.name}</span><span>${p.score}</span></div>`)
    .join('');
}
