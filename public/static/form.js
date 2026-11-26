"use strict";
// Explicit offsets make historical timestamps independent of the server timezone.
function toUtcMinute(localValue, offsetValue) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(localValue) ||
      !/^[+-]\d{2}:\d{2}$/.test(offsetValue)) throw new Error("Enter a date, time and UTC offset.");
  const hours = Number(offsetValue.slice(1, 3));
  const minutes = Number(offsetValue.slice(4));
  if (hours > 14 || minutes > 59 || (hours === 14 && minutes !== 0)) throw new Error("Invalid UTC offset.");
  const wall = new Date(localValue + ":00Z");
  if (!Number.isFinite(wall.getTime()) || wall.toISOString().slice(0, 16) !== localValue)
    throw new Error("Invalid observation date or time.");
  const direction = offsetValue[0] === "+" ? 1 : -1;
  const utc = new Date(wall.getTime() - direction * (hours * 60 + minutes) * 60000);
  const iso = utc.toISOString();
  if (!/^\d{4}-/.test(iso)) throw new Error("Observation date is outside the supported range.");
  return iso.slice(0, 16) + ":00Z";
}
if (typeof module !== "undefined") module.exports = {toUtcMinute};
if (typeof document !== "undefined") {
  const local = document.getElementById("local-time");
  const offset = document.getElementById("utc-offset");
  const stored = document.getElementById("timestamp_utc");
  const preview = document.getElementById("utc-preview");
  const pad = n => String(n).padStart(2, "0");
  function deviceOffset() {
    const date = new Date(local.value);
    if (!Number.isFinite(date.getTime())) return;
    const value = -date.getTimezoneOffset();
    offset.value = (value < 0 ? "-" : "+") + pad(Math.floor(Math.abs(value) / 60)) + ":" + pad(Math.abs(value) % 60);
  }
  function sync() {
    offset.setCustomValidity("");
    try { stored.value = toUtcMinute(local.value, offset.value); preview.textContent = stored.value; }
    catch (error) { stored.value = ""; preview.textContent = "Invalid date or offset"; offset.setCustomValidity(error.message); }
  }
  if (stored.value) {
    // On a validation retry preserve the exact instant, displayed explicitly in UTC.
    local.value = stored.value.slice(0, 16); offset.value = "+00:00";
  } else {
    const now = new Date();
    local.value = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
    deviceOffset();
  }
  let manualOffset = Boolean(stored.value);
  offset.addEventListener("input", () => { manualOffset = true; sync(); });
  local.addEventListener("input", () => { if (!manualOffset) deviceOffset(); sync(); });
  document.getElementById("record-form").addEventListener("submit", event => {
    sync();
    if (!stored.value) { event.preventDefault(); offset.reportValidity(); }
  });
  sync();
}
