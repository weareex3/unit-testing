// 57 personal symbols (n=7 projective-plane construction: 57 cards, 8 symbols/card,
// every two cards share exactly one symbol).
const SYMBOLS = [
  { emoji: '🥗', label: 'Caesar Salad' },
  { emoji: '🥯', label: 'Bagels' },
  { emoji: '🦃', label: 'Sliced Turkey' },
  { emoji: '🍕', label: 'Pizza' },
  { emoji: '🍝', label: 'Pasta' },
  { emoji: '🍣', label: 'Sushi' },
  { emoji: '🍨', label: 'Amorino' },
  { emoji: '🍜', label: 'Wagamama Teriyaki' },
  { emoji: '🧀', label: 'Mac & Cheese' },
  { emoji: '🛒', label: "Trader Joe's" },
  { emoji: '🟢', label: 'Green M&M' },
  { emoji: '🤍', label: 'Burrata' },
  { emoji: '🧈', label: 'Parmesan' },
  { emoji: '🍛', label: 'Butter Chicken (Dishoom)' },
  { emoji: '🍦', label: 'Ice Cream' },
  { emoji: '🥤', label: 'Diet Coke' },
  { emoji: '☕', label: 'Hot Chocolate' },
  { emoji: '🎊', label: 'Multicolour Sprinkles' },
  { emoji: '🥑', label: 'Avocado Toast' },
  { emoji: '🥂', label: 'Hugo Spritz' },
  { emoji: '🧋', label: 'Iced Coffee' },
  { emoji: '🍰', label: 'Cookies & Cream Cake' },
  { emoji: '🥡', label: 'Pad Thai' },
  { emoji: '🍭', label: 'Orange Calippo' },
  { emoji: '🍗', label: "Chicken Tenders (McDonald's)" },
  { emoji: '🏛️', label: 'Colosseum' },
  { emoji: '🚇', label: 'London Underground' },
  { emoji: '🏙️', label: 'Empire State Building' },
  { emoji: '🛶', label: 'Venice' },
  { emoji: '🌉', label: 'Brooklyn Bridge' },
  { emoji: '🏞️', label: 'Lake (Montanejos)' },
  { emoji: '✈️', label: 'Plane' },
  { emoji: '🐦', label: 'Mockingjay' },
  { emoji: '🧙‍♀️', label: 'Elphaba' },
  { emoji: '❄️', label: 'Elsa' },
  { emoji: '🎭', label: 'Hamilton' },
  { emoji: '🕶️', label: 'Men In Black' },
  { emoji: '🐨', label: 'Koalas' },
  { emoji: '🐶', label: 'Beige Cavapoo' },
  { emoji: '🐩', label: 'Beige Labradoodle' },
  { emoji: '🐹', label: 'Lemming' },
  { emoji: '🥔', label: 'A Furry Potato' },
  { emoji: '🐄', label: 'Smudge (Jellycat Cow)' },
  { emoji: '💃', label: 'Dancer' },
  { emoji: '🩰', label: 'Dance Shoes' },
  { emoji: '🧸', label: 'Ballerina Teddy Bear' },
  { emoji: '🐰', label: 'Bunny Teddy Bear' },
  { emoji: '🏷️', label: 'Jellycat Logo' },
  { emoji: '🐓', label: 'Spurs' },
  { emoji: '👑', label: 'Crown Hat' },
  { emoji: '💌', label: 'A + L' },
  { emoji: '🏕️', label: "Pinemere's Camp" },
  { emoji: '🪑', label: 'Park Bench' },
  { emoji: '📖', label: 'Book' },
  { emoji: '📲', label: 'Kindle' },
  { emoji: '🥷', label: 'Ninja Warrior Kit' },
  { emoji: '🧪', label: 'Science Kit' },
];

// Classic finite-projective-plane construction (n must be prime).
// Produces n^2+n+1 cards from n^2+n+1 symbols, each card holding n+1 symbols,
// with every pair of cards sharing exactly one symbol.
function generateDeck(n) {
  const deck = [];
  for (let i = 0; i <= n; i++) {
    const card = [0];
    for (let j = 0; j < n; j++) card.push(1 + i * n + j);
    deck.push(card);
  }
  for (let i = 0; i < n; i++) {
    for (let j = 0; j < n; j++) {
      const card = [1 + i];
      for (let k = 0; k < n; k++) {
        card.push(1 + n + n * k + ((i * k + j) % n));
      }
      deck.push(card);
    }
  }
  return deck;
}

const N = 7; // -> 57 symbols, 57 cards, 8 symbols/card
const DECK = generateDeck(N);

module.exports = { SYMBOLS, DECK, N };
