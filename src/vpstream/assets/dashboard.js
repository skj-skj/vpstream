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
        if (!response.ok) {
          var error = new Error(data.error || ("error " + response.status));
          error.data = data;
          throw error;
        }
        return data;
      });
    });
  }

  function planText(plans) {
    return (plans || []).map(function (p) {
      return p.name + ": about " + duration(p.seconds) + " for a " + duration(p.duration) +
        " video (" + p.tasks.map(function (t) { return t.task; }).join(", ") + ")";
    }).join("\n");
  }

  // A change that would take long (a transcode, a prepare longer than the film) is refused
  // with code "confirm"; it is sent again with confirmed once the user agrees.
  function change(route, payload, then) {
    if (busy) { return; }
    busy = true;
    $("error").textContent = "";
    var body = payload || {};
    var again = false;
    request("POST", route, body).then(function (info) {
      render(info);
      if (then) { then(info); }
    }).catch(function (error) {
      if (error.data && error.data.code === "confirm" && !body.confirmed &&
          window.confirm(error.message + "\n\n" + planText(error.data.plans) + "\n\nGo ahead?")) {
        again = true;
        return;
      }
      $("error").textContent = error.message;
    }).then(function () {
      busy = false;
      if (again) {
        body.confirmed = true;
        change(route, body, then);
      }
    });
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
      var name = video.name + (video.preparing ? " (preparing a seek index)" : "") +
        (video.job ? " (" + jobState(video.job) + ")" : video.ready === false ? " (not ready)" : "") +
        (video.note ? " (" + video.note + ")" : "");
      rows.push(row([stream.kind === "dir" ? video.number : "", el("td", url, "url"), name]));
    });
    fill($("videos"), rows);
  }

  function renderDevices(stream, now) {
    var head = row(["Address", "State", "Code", "Requests", "Blocked", "Sent", "Last seen", "Player", ""], "th");
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
      return row([d.ip, deviceState(d), d.code || "-", d.requests, d.blocked, size(d.bytes), seen,
        d.user_agent || "-", actions]);
    });
    if (!rows.length) { rows = [row(["No device has asked for the stream yet.", "", "", "", "", "", "", "", ""])]; }
    fill($("devices"), [head].concat(rows));
  }

  function jobState(job) {
    var left = job.seconds_left === null ? "" : ", about " + duration(job.seconds_left) + " left";
    if (job.state === "running") { return Math.round(100 * job.progress) + "%" + left; }
    if (job.state === "queued") { return "queued" + left; }
    return job.state + (job.error ? ": " + job.error : "");
  }

  function renderJobs(jobs) {
    var head = row(["Video", "Version", "State", ""], "th");
    var rows = jobs.map(function (job) {
      var action = job.state === "queued" || job.state === "running" ?
        button("Cancel", function () { change("prepare/cancel", { path: job.source }); }, "danger") : "";
      return row([job.name, job.task, jobState(job), action]);
    });
    fill($("jobs"), rows.length ? [head].concat(rows) : []);
  }

  function renderCache(list) {
    var head = row(["Video", "Base", "Soft", "Burned in", "Size", ""], "th");
    var rows = list.map(function (e) {
      var marks = (e.stale ? " (stale)" : "") + (e.in_use ? " (in use)" : "") + (e.busy ? " (preparing)" : "");
      var rm = e.in_use || e.busy ? "" : button("Remove", function () {
        if (window.confirm("Remove the prepared versions of " + e.source + "?")) {
          change("prepared/rm", { path: e.source }, loadCache);
        }
      }, "danger");
      return row([e.source + marks, e.base || "-", e.soft.join(",") || "-", e.burn.join(",") || "-",
        size(e.bytes), rm]);
    });
    fill($("cache"), rows.length ? [head].concat(rows) : [row(["Nothing is prepared."])]);
  }

  function loadCache() {
    request("GET", "prepared").then(function (data) { renderCache(data.prepared); })
      .catch(function (error) { $("error").textContent = error.message; });
  }

  function render(info) {
    var stream = info.stream;
    renderJobs(info.prepare || []);
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
    var options = { ttl: ttl === "" ? null : Number(ttl), mode: $("mode").value };
    options[$("kind").value] = $("path").value.trim();
    ["alias", "policy", "audio", "sub"].forEach(function (name) {
      var value = $(name).value.trim();
      if (value && (options.mode === "browser" || (name !== "audio" && name !== "sub"))) {
        options[name] = value;
      }
    });
    change("stream/start", options);
  });
  function showBrowserFields() {
    Array.prototype.forEach.call(document.querySelectorAll("label.browser"), function (l) {
      l.hidden = $("mode").value !== "browser";
    });
  }
  $("mode").addEventListener("change", showBrowserFields);
  showBrowserFields();
  // A prepare is shown first: what it makes of each video, how long that takes, and the
  // tracks it found; "Prepare it" then queues that plan.
  var planned = null;

  function renderPlan(plans) {
    var rows = [row(["Video", "What it makes", "About", "Tracks"], "th")];
    plans.forEach(function (p) {
      var made = p.tasks.length ? p.tasks.map(function (t) {
        return t.task + " (" + duration(t.seconds) + ")";
      }).join(", ") : "nothing to make";
      var about = p.unknown ? "unknown" : duration(p.seconds) + " for " + duration(p.duration) +
        (p.longer_than_film ? " (longer than the film)" : "");
      var tracks = p.tracks.map(function (t) {
        return t.kind + " " + t.number + ": " + t.label + " (" + t.treatment + ")";
      }).join("; ");
      rows.push(row([p.name, made + (p.notes.length ? " · " + p.notes.join("; ") : ""), about, tracks]));
    });
    fill($("plantable"), rows);
  }

  $("prepareform").addEventListener("submit", function (event) {
    event.preventDefault();
    var options = { dry_run: true };
    options[$("pkind").value] = $("ppath").value.trim();
    if ($("psub").value.trim()) { options.sub = $("psub").value.trim(); }
    change("prepare", options, function (info) {
      delete options.dry_run;
      planned = options;
      renderPlan(info.plans || []);
      $("plan").hidden = false;
    });
  });
  $("queue").addEventListener("click", function () {
    if (!planned) { return; }
    var options = planned;
    options.confirmed = true;  // the plan, with its time, was shown above
    planned = null;
    $("plan").hidden = true;
    change("prepare", options);
  });
  $("dropplan").addEventListener("click", function () { planned = null; $("plan").hidden = true; });
  $("reload").addEventListener("click", loadCache);
  $("rmstale").addEventListener("click", function () { change("prepared/rm", { stale: true }, loadCache); });
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
