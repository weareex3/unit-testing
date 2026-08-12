const socket = io();
let roomCode = null;
let roundActive = false;

const el = (id) => document.getElementById(id);
const joinPanel = el('joinPanel');
const waitingPanel = el('waitingPanel');
const gameArea = el('gameArea');
const gameOver = el('gameOver');
const resultOverlay = el('resultOverlay');

const pathCode = window.location.pathname.split('/play/')[1];
if (pathCode) el('codeInput').value = pathCode.toUpperCase();

el('joinBtn').addEventListener('click', join);
el('nameInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') join(); });

function join() {
  const code = el('codeInput').value.trim().toUpperCase();
  const name = el('nameInput').value.trim();
  if (!code || !name) {
    el('joinError').textContent = 'Enter a room code and your name.';
    return;
  }
  socket.emit('player:join', { code, name }, (res) => {
    if (!res.ok) {
      el('joinError').textContent = res.error || 'Could not join.';
      return;
    }
    roomCode = code;
    joinPanel.classList.add('hidden');
    waitingPanel.classList.remove('hidden');
  });
}

socket.on('round:new', (data) => {
  roundActive = true;
  waitingPanel.classList.add('hidden');
  gameOver.classList.add('hidden');
  gameArea.classList.remove('hidden');
  el('roundNum').textContent = data.roundNumber;
  el('totalRounds').textContent = data.totalRounds;
  renderCard(el('cardA'), data.cardA, { onTap: handleTap });
  renderCard(el('cardB'), data.cardB, { onTap: handleTap });
});

function handleTap(symbolId, node) {
  if (!roundActive) return;
  socket.emit('player:answer', { code: roomCode, symbolId });
  node.classList.add('correct');
  setTimeout(() => node.classList.remove('correct'), 600);
}

socket.on('round:result', (data) => {
  roundActive = false;
  const won = data.winnerName === el('nameInput').value.trim();
  showOverlay(won ? '🎉' : '👀', won ? 'You got it!' : `${data.winnerName} found it!`, `${data.emoji} ${data.label}`);
});

socket.on('round:timeout', (data) => {
  roundActive = false;
  showOverlay('⏰', "Time's up!", `It was ${data.emoji} ${data.label}`);
});

socket.on('game:over', (data) => {
  gameArea.classList.add('hidden');
  gameOver.classList.remove('hidden');
  const medals = ['🥇', '🥈', '🥉'];
  el('finalLeaderboard').innerHTML = data.scores
    .map((p, i) => `<div class="leaderboard-row"><span>${medals[i] || ''} ${p.name}</span><span>${p.score}</span></div>`)
    .join('');
});

function showOverlay(emoji, title, label) {
  el('resultEmoji').textContent = emoji;
  el('resultTitle').textContent = title;
  el('resultLabel').textContent = label;
  resultOverlay.classList.remove('hidden');
  setTimeout(() => resultOverlay.classList.add('hidden'), 2000);
}
