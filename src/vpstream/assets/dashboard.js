// The vpstream dashboard. It shows what the daemon's status says and sends changes to the
// same API the command line uses. Text from the server goes in with textContent only.
(function () {
  "use strict";
  var API = "/api/v1/";
  var REFRESH_MS = 2000;
  var ACTIVE_SECONDS = 30; // a device that asked for something this recently is "connected"
  var POLICY_TEXT = {
    open: "Everyone gets in, except the devices you disable.",
    approval: "Only the devices you approve get in. The others wait here, marked WL.",
    locked: "Only the enabled devices get in. Unlock to add another."
  };
  var busy = false;

  function $(id) { return document.getElementById(id); }

  function el(tag, text, cls) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    if (cls) { node.className = cls; }
    return node;
  }

  function fill(node, children) {
    while (node.firstChild) { node.removeChild(node.firstChild); }
    children.forEach(function (child) { node.appendChild(child); });
  }

  function row(cells, tag) {
    var tr = document.createElement("tr");
    cells.forEach(function (cell) {
      if (cell instanceof Node && /^T[DH]$/.test(cell.nodeName)) { tr.appendChild(cell); return; }
      var td = document.createElement(tag || "td");
      if (cell instanceof Node) { td.appendChild(cell); } else { td.textContent = String(cell); }
      tr.appendChild(td);
    });
    return tr;
  }

  function duration(seconds) {
    var s = Math.max(0, Math.floor(seconds));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    if (h) { return h + "h " + (m < 10 ? "0" : "") + m + "m"; }
    if (m) { return m + "m " + (s % 60 < 10 ? "0" : "") + (s % 60) + "s"; }
    return s + "s";
  }

  function size(bytes) {
    var units = ["B", "kB", "MB", "GB"], i = 0, n = bytes || 0;
    while (n >= 1000 && i < units.length - 1) { n /= 1000; i += 1; }
    return (i ? n.toFixed(1) : String(n)) + " " + units[i];
  }

  function deviceState(d) {
    var state;
    if (d.allowed === null) { state = d.passes ? "allowed" : "waiting"; }
    else { state = d.allowed ? "enabled" : "disabled"; }
    return d.wl ? state + " · WL" : state;
  }

  function request(method, route, payload) {
    var options = { method: method, headers: {} };
    if (payload !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }
    return fetch(API + route, options).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) { throw new Error(data.error || ("error " + response.status)); }
        return data;
      });
    });
  }

  function change(route, payload) {
    if (busy) { return; }
    busy = true;
    $("error").textContent = "";
    request("POST", route, payload || {}).then(render).catch(function (error) {
      $("error").textContent = error.message;
    }).then(function () { busy = false; });
  }

  function button(text, onclick, cls) {
    var b = el("button", text, cls);
    b.type = "button";
    b.addEventListener("click", onclick);
    return b;
  }

  function renderVideos(stream) {
    var rows = [];
    if (stream.kind === "dir") {
      rows.push(row(["all", el("td", stream.url, "url"), "index for browsers, playlist for players"]));
    }
    stream.videos.forEach(function (video) {
      var url = stream.kind === "dir" ? video.url : stream.url;
      rows.push(row([stream.kind === "dir" ? video.number : "", el("td", url, "url"), video.name]));
    });
    fill($("videos"), rows);
  }

  function renderDevices(stream, now) {
    var head = row(["Address", "State", "Requests", "Blocked", "Sent", "Last seen", "Player", ""], "th");
    var rows = stream.devices.map(function (d) {
      var ago = now - d.last_seen;
      var seen = !d.last_seen ? "never" : ago < ACTIVE_SECONDS ? "now" : duration(ago) + " ago";
      var actions = el("span");
      var yes = stream.policy === "approval" ? "Approve" : "Enable";
      var no = stream.policy === "approval" ? "Deny" : "Disable";
      if (d.allowed !== true) {
        actions.appendChild(button(yes, function () { change("devices", { ip: d.ip, allowed: true }); }));
      }
      if (d.allowed !== false) {
        actions.appendChild(button(no, function () { change("devices", { ip: d.ip, allowed: false }); }, "danger"));
      }
      return row([d.ip, deviceState(d), d.requests, d.blocked, size(d.bytes), seen, d.user_agent || "-", actions]);
    });
    if (!rows.length) { rows = [row(["No device has asked for the stream yet.", "", "", "", "", "", "", ""])]; }
    fill($("devices"), [head].concat(rows));
  }

  function render(info) {
    var stream = info.stream;
    var notes = [];
    $("version").textContent = info.version || "";
    if (info.firewall_problem) { notes.push(el("div", "Firewall: " + info.firewall_problem)); }
    if (stream && stream.guessers.length) {
      notes.push(el("div", "Shut out for trying addresses: " + stream.guessers.join(", ") +
        " (enable one with: vpstream devices enable ADDRESS)"));
    }
    fill($("notes"), notes);
    $("idle").hidden = !!stream;
    $("live").hidden = !stream;
    if (!stream) { return; }
    var left = stream.seconds_left === null ? "no time limit" : "stops in " + duration(stream.seconds_left);
    $("summary").textContent = "/" + stream.alias + " · " + stream.mode + " · " + left +
      " · port " + info.stream_port + " " + info.stream_port_state;
    renderVideos(stream);
    Array.prototype.forEach.call($("policies").querySelectorAll("button"), function (b) {
      var policy = b.getAttribute("data-policy");
      b.className = policy === stream.policy ? "on" : "";
      if (policy === "locked") { b.textContent = stream.policy === "locked" ? "Unlock" : "Lock"; }
    });
    $("policytext").textContent = POLICY_TEXT[stream.policy] || "";
    renderDevices(stream, info.now);
  }

  function refresh() {
    if (busy || document.hidden) { return; }
    request("GET", "status").then(render).catch(function (error) {
      $("error").textContent = "The daemon does not answer: " + error.message;
    });
  }

  $("start").addEventListener("submit", function (event) {
    event.preventDefault();
    // An empty field is the default time limit, never "no limit".
    var ttl = $("ttl").value.trim();
    var options = { ttl: ttl === "" ? null : Number(ttl), policy: $("policy").value };
    options[$("kind").value] = $("path").value.trim();
    if ($("alias").value.trim()) { options.alias = $("alias").value.trim(); }
    change("stream/start", options);
  });
  $("stop").addEventListener("click", function () {
    if (window.confirm("Stop the stream and close its port?")) { change("stream/stop"); }
  });
  $("setalias").addEventListener("click", function () {
    change("stream/alias", { alias: $("newalias").value.trim() });
    $("newalias").value = "";
  });
  $("policies").addEventListener("click", function (event) {
    var policy = event.target.getAttribute && event.target.getAttribute("data-policy");
    if (!policy) { return; }
    if (policy === "locked" && event.target.textContent === "Unlock") { change("stream/unlock"); }
    else { change("stream/policy", { policy: policy }); }
  });

  refresh();
  window.setInterval(refresh, REFRESH_MS);
})();
