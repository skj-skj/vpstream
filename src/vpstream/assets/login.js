// The public dashboard's login. The password never leaves the page: the server sends a
// one-time nonce with the salt and iteration count, the page derives the key with
// PBKDF2-HMAC-SHA256 and answers with HMAC-SHA256(key, label + nonce). See login.py.
(function () {
  "use strict";
  var API = "/api/v1/";
  var KEY_BYTES = 32;
  var C = window.vpCrypto;

  function $(id) { return document.getElementById(id); }

  function post(route, payload) {
    return fetch(API + route, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      credentials: "same-origin"
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) {
          var error = new Error(data.error || ("error " + response.status));
          error.status = response.status;
          throw error;
        }
        return data;
      });
    });
  }

  function plainKey(password, salt, iterations) {
    // A moment first, so "Checking" shows before the page is busy for a second or two.
    return new Promise(function (resolve) {
      window.setTimeout(function () { resolve(C.pbkdf2(password, salt, iterations, KEY_BYTES)); }, 30);
    });
  }

  // The key: with WebCrypto where the page has it (a secure page), else in plain JavaScript.
  function deriveKey(text, saltHex, iterations) {
    var password = C.utf8(text.normalize ? text.normalize("NFC") : text);
    var salt = C.unhex(saltHex);
    var subtle = window.isSecureContext && window.crypto && window.crypto.subtle;
    if (!subtle) { return plainKey(password, salt, iterations); }
    return subtle.importKey("raw", password, "PBKDF2", false, ["deriveBits"]).then(function (key) {
      return subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: salt, iterations: iterations }, key, KEY_BYTES * 8);
    }).then(function (bits) { return new Uint8Array(bits); }, function () {
      return plainKey(password, salt, iterations);
    });
  }

  function state(text, bad, busy) {
    $("message").textContent = text;
    $("message").className = "small " + (bad ? "error" : "muted");
    $("go").disabled = busy;
  }

  $("login").addEventListener("submit", function (event) {
    event.preventDefault();
    var password = $("password").value;
    if (!password) { return; }
    state("Checking the password...", false, true);
    post("login/challenge", {}).then(function (challenge) {
      return deriveKey(password, challenge.salt, challenge.iterations).then(function (key) {
        var proof = C.hex(C.hmac(key, C.utf8(challenge.label + challenge.nonce)));
        return post("login", { nonce: challenge.nonce, proof: proof });
      });
    }).then(function () {
      $("password").value = "";
      state("Logged in", false, true);
      window.location.replace("/");
    }).catch(function (error) {
      $("password").value = "";
      state(error.message, true, false);
      $("password").focus();
    });
  });
})();
