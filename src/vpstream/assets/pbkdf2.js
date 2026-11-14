// SHA-256, HMAC-SHA256 and PBKDF2-HMAC-SHA256 in plain JavaScript, for the login page when
// the browser has no WebCrypto (crypto.subtle), as on a page it does not count as secure.
// Bytes are Uint8Arrays. Checked against Python's hashlib with known vectors (test_login.py).
(function () {
  "use strict";
  var K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
  ];
  var START = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
  var W = new Int32Array(64);

  // One block of 16 words into `state` (8 words), in place.
  function compress(state, block) {
    var i, s0, s1, t1, t2;
    for (i = 0; i < 16; i++) { W[i] = block[i]; }
    for (i = 16; i < 64; i++) {
      var a15 = W[i - 15], a2 = W[i - 2];
      s0 = ((a15 >>> 7) | (a15 << 25)) ^ ((a15 >>> 18) | (a15 << 14)) ^ (a15 >>> 3);
      s1 = ((a2 >>> 17) | (a2 << 15)) ^ ((a2 >>> 19) | (a2 << 13)) ^ (a2 >>> 10);
      W[i] = (W[i - 16] + s0 + W[i - 7] + s1) | 0;
    }
    var a = state[0], b = state[1], c = state[2], d = state[3];
    var e = state[4], f = state[5], g = state[6], h = state[7];
    for (i = 0; i < 64; i++) {
      s1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
      t1 = (h + s1 + ((e & f) ^ (~e & g)) + K[i] + W[i]) | 0;
      s0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
      t2 = (s0 + ((a & b) ^ (a & c) ^ (b & c))) | 0;
      h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
    }
    state[0] = (state[0] + a) | 0; state[1] = (state[1] + b) | 0;
    state[2] = (state[2] + c) | 0; state[3] = (state[3] + d) | 0;
    state[4] = (state[4] + e) | 0; state[5] = (state[5] + f) | 0;
    state[6] = (state[6] + g) | 0; state[7] = (state[7] + h) | 0;
  }

  function words(bytes, at) {
    var out = new Int32Array(16);
    for (var i = 0; i < 16; i++) {
      var j = at + 4 * i;
      out[i] = (bytes[j] << 24) | (bytes[j + 1] << 16) | (bytes[j + 2] << 8) | bytes[j + 3];
    }
    return out;
  }

  function toBytes(state) {
    var out = new Uint8Array(32);
    for (var i = 0; i < 8; i++) {
      out[4 * i] = state[i] >>> 24; out[4 * i + 1] = state[i] >>> 16;
      out[4 * i + 2] = state[i] >>> 8; out[4 * i + 3] = state[i];
    }
    return out;
  }

  // The state after `prefix` (whole blocks) and then `bytes`, padded as SHA-256 pads.
  function hashFrom(state, prefixBytes, bytes) {
    var total = prefixBytes + bytes.length;
    var padded = new Uint8Array(Math.ceil((bytes.length + 9) / 64) * 64);
    padded.set(bytes);
    padded[bytes.length] = 0x80;
    var bits = total * 8, end = padded.length;
    padded[end - 1] = bits; padded[end - 2] = bits >>> 8; padded[end - 3] = bits >>> 16;
    padded[end - 4] = bits >>> 24; padded[end - 5] = Math.floor(bits / 0x100000000);
    for (var at = 0; at < end; at += 64) { compress(state, words(padded, at)); }
    return state;
  }

  function sha256(bytes) { return toBytes(hashFrom(START.slice(), 0, bytes)); }

  // The states after the inner and outer pads of HMAC with `key`.
  function pads(key) {
    if (key.length > 64) { key = sha256(key); }
    var inner = new Uint8Array(64), outer = new Uint8Array(64);
    for (var i = 0; i < 64; i++) { inner[i] = (key[i] || 0) ^ 0x36; outer[i] = (key[i] || 0) ^ 0x5c; }
    var si = START.slice(), so = START.slice();
    compress(si, words(inner, 0)); compress(so, words(outer, 0));
    return [si, so];
  }

  function hmac(key, message) {
    var p = pads(key);
    var inner = toBytes(hashFrom(p[0].slice(), 64, message));
    return toBytes(hashFrom(p[1].slice(), 64, inner));
  }

  // PBKDF2-HMAC-SHA256 for `length` bytes. Each round after the first hashes 32 bytes after a
  // pad: one block with fixed padding, made once.
  function pbkdf2(password, salt, iterations, length) {
    var p = pads(password), out = new Uint8Array(length);
    var block = new Int32Array(16), state = new Int32Array(8), sum = new Int32Array(8);
    block[8] = 0x80000000; block[15] = (64 + 32) * 8;
    for (var n = 1; n * 32 - 32 < length; n++) {
      var first = new Uint8Array(salt.length + 4);
      first.set(salt);
      first[salt.length] = n >>> 24; first[salt.length + 1] = n >>> 16;
      first[salt.length + 2] = n >>> 8; first[salt.length + 3] = n;
      var u = hmac(password, first), i, r;
      for (i = 0; i < 8; i++) { sum[i] = (u[4 * i] << 24) | (u[4 * i + 1] << 16) | (u[4 * i + 2] << 8) | u[4 * i + 3]; }
      var last = sum.slice();
      for (r = 1; r < iterations; r++) {
        for (i = 0; i < 8; i++) { block[i] = last[i]; state[i] = p[0][i]; }
        compress(state, block);
        for (i = 0; i < 8; i++) { block[i] = state[i]; state[i] = p[1][i]; }
        compress(state, block);
        for (i = 0; i < 8; i++) { last[i] = state[i]; sum[i] ^= state[i]; }
      }
      out.set(toBytes(sum).subarray(0, Math.min(32, length - (n - 1) * 32)), (n - 1) * 32);
    }
    return out;
  }

  // UTF-8 bytes of a string; the password is first put in NFC, as the server does.
  function utf8(text) {
    var bytes = [];
    for (var i = 0; i < text.length; i++) {
      var c = text.codePointAt(i);
      if (c > 0xffff) { i++; }
      if (c < 0x80) { bytes.push(c); }
      else if (c < 0x800) { bytes.push(0xc0 | (c >> 6), 0x80 | (c & 63)); }
      else if (c < 0x10000) { bytes.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63)); }
      else { bytes.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 63), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63)); }
    }
    return new Uint8Array(bytes);
  }

  function hex(bytes) {
    var out = "";
    for (var i = 0; i < bytes.length; i++) { out += (bytes[i] < 16 ? "0" : "") + bytes[i].toString(16); }
    return out;
  }

  function unhex(text) {
    var out = new Uint8Array(text.length / 2);
    for (var i = 0; i < out.length; i++) { out[i] = parseInt(text.substr(2 * i, 2), 16); }
    return out;
  }

  window.vpCrypto = { sha256: sha256, hmac: hmac, pbkdf2: pbkdf2, utf8: utf8, hex: hex, unhex: unhex };
})();
