// calc.js — checkout price calculator
const TAX = 0.05;
module.exports = { total: (p, q) => p * q + TAX }; // BUG: adds flat tax, should be 5% of subtotal
