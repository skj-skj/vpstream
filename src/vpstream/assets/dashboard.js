// The vpstream dashboard. It shows what the daemon's status says and sends changes to the
// same API the command line uses. Text from the server goes in with textContent only.
(function () {
  "use strict";
  var API = "/api/v1/";
  var REFRESH_MS = 2000;
  var CACHE_MS = 10000;
  var ACTIVE_SECONDS = 30; // a device that asked for something this recently is watching
  var POLICY_TEXT = {
    open: "Everyone with the address gets in, except the devices you disable.",
    approval: "Only the devices you approve get in. New ones wait with a code.",
    locked: "Only the devices let in already. Unlock to add another."
  };
  var busy = false;
  var tab = "stream";
  var last = null;  // the last status
  var cacheAt = 0;
  var allJobs = false;  // finished prepares: the last few, or all the daemon keeps
  var RECENT = 8;

  function $(id) { return document.getElementById(id); }

  // An element: h("div", {className: "x", onclick: f}, [children or text]).
  function h(tag, props, children) {
    var node = document.createElement(tag);
    Object.keys(props || {}).forEach(function (key) {
      if (key.slice(0, 2) === "on") { node.addEventListener(key.slice(2), props[key]); }
      else if (key === "style") { node.setAttribute("style", props[key]); }
      else { node[key] = props[key]; }
    });
    [].concat(children === undefined ? [] : children).forEach(function (child) {
      if (child === null || child === undefined || child === false) { return; }
      node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
    });
    return node;
  }

  function fill(node, children) {
    while (node.firstChild) { node.removeChild(node.firstChild); }
    children.forEach(function (child) { if (child) { node.appendChild(child); } });
  }

  function duration(seconds) {
    var s = Math.max(0, Math.floor(seconds || 0));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    if (h >= 24) { return Math.floor(h / 24) + "d " + (h % 24) + "h"; }
    if (h) { return h + "h " + (m < 10 ? "0" : "") + m + "m"; }
    if (m) { return m + "m " + (s % 60 < 10 ? "0" : "") + (s % 60) + "s"; }
    return s + "s";
  }

  function size(bytes) {
    var units = ["B", "kB", "MB", "GB", "TB"], i = 0, n = bytes || 0;
    while (n >= 1000 && i < units.length - 1) { n /= 1000; i += 1; }
    return (i ? n.toFixed(1) : String(n)) + " " + units[i];
  }

  function pill(text, kind) { return h("span", { className: "pill " + (kind || "") }, text); }

  function bar(fraction) {
    var fillBar = h("i", { style: "width:" + Math.round(100 * Math.max(0, Math.min(1, fraction))) + "%" });
    return h("div", { className: "progress" }, fillBar);
  }

  function button(text, onclick, cls) {
    return h("button", { type: "button", className: "btn sm " + (cls || ""), onclick: onclick }, text);
  }

  function empty(title, text) { return h("div", { className: "empty" }, [h("b", {}, title), text]); }

  // --- talking to the daemon

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

  function toast(text, bad) {
    var box = h("div", { className: "toast" + (bad ? " bad" : "") }, text);
    $("toasts").appendChild(box);
    window.setTimeout(function () { box.remove(); }, bad ? 7000 : 3000);
  }

  // A question in a sheet; `then` runs on OK.
  function ask(title, text, yes, then, danger) {
    $("modaltitle").textContent = title;
    $("modaltext").textContent = text;
    $("modalyes").textContent = yes;
    $("modalyes").className = "btn " + (danger ? "danger solid" : "primary");
    $("modal").hidden = false;
    $("modalyes").onclick = function () { $("modal").hidden = true; then(); };
    $("modalno").onclick = function () { $("modal").hidden = true; };
  }

  function planText(plans) {
    return (plans || []).filter(function (p) { return p.tasks.length; }).map(function (p) {
      return p.name + ": about " + duration(p.seconds) + " for a " + duration(p.duration) + " video";
    }).join("\n");
  }

  // A change that would take long (a transcode, a prepare longer than the film) is refused
  // with code "confirm"; it is sent again with confirmed once the user agrees.
  function change(route, payload, then, done) {
    if (busy) { return; }
    busy = true;
    var body = payload || {};
    request("POST", route, body).then(function (info) {
      busy = false;
      render(info);
      if (done) { toast(done); }
      if (then) { then(info); }
    }).catch(function (error) {
      busy = false;
      if (error.data && error.data.code === "confirm" && !body.confirmed) {
        ask("This takes a while", error.message + "\n\n" + planText(error.data.plans), "Go ahead",
          function () { body.confirmed = true; change(route, body, then, done); });
        return;
      }
      toast(error.message, true);
    });
  }

  // --- a segmented control: one button "on"

  function segment(id, value) {
    Array.prototype.forEach.call($(id).querySelectorAll("button"), function (b) {
      b.className = (b.getAttribute("data-value") || b.getAttribute("data-policy")) === value ? "on" : "";
    });
  }

  function segmentValue(id) {
    var on = $(id).querySelector("button.on");
    return on ? on.getAttribute("data-value") : "";
  }

  function segmented(id, onchange) {
    $(id).addEventListener("click", function (event) {
      var b = event.target.closest("button");
      if (!b) { return; }
      segment(id, b.getAttribute("data-value"));
      if (onchange) { onchange(b.getAttribute("data-value")); }
    });
  }

  // --- the stream

  function jobState(job) {
    var left = job.seconds_left === null ? "" : ", about " + duration(job.seconds_left) + " left";
    if (job.state === "running") { return Math.round(100 * job.progress) + "%" + left; }
    if (job.state === "queued") { return "queued" + left; }
    return job.state + (job.error ? ": " + job.error : "");
  }

  function videoState(video) {
    if (video.job) {
      return [pill(video.job.state === "queued" ? "Queued" : "Preparing " + Math.round(100 * video.job.progress) + "%", "warn"),
        video.job.state === "running" ? bar(video.job.progress) : null];
    }
    if (video.preparing) { return [pill("Making a seek index", "warn"), null]; }
    if (video.ready === false) { return [pill("Not ready", "bad"), null]; }
    return [pill("Ready", "ok"), null];
  }

  function copy(text) {
    var done = function () { toast("Address copied"); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { toast(text); });
    } else {
      toast(text);
    }
  }

  function renderVideos(stream) {
    var folder = stream.kind === "dir";
    var items = stream.videos.map(function (video) {
      var state = videoState(video);
      var url = folder ? video.url : stream.url;
      return h("div", { className: "item" }, [
        h("div", { className: "num" }, folder ? video.number : "1"),
        h("div", { className: "grow" }, [
          h("div", { className: "title ellipsis" }, video.name),
          h("div", { className: "sub mono ellipsis" }, url),
          video.note ? h("div", { className: "sub" }, video.note) : null,
          state[1]
        ]),
        h("div", { className: "actions" }, [state[0], button("Copy", function () { copy(url); })])
      ]);
    });
    $("videocount").textContent = folder ? stream.videos.length + " · index at /" + stream.alias : "";
    fill($("videos"), items);
  }

  function renderStream(info) {
    var stream = info.stream;
    $("idle").hidden = !!stream;
    $("live").hidden = !stream;
    if (!stream) { return; }
    $("aliasname").textContent = "/" + stream.alias;
    $("modepill").textContent = stream.mode === "browser" ? "Browser mode" : "Direct mode";
    $("url").textContent = stream.url;
    $("left").textContent = stream.seconds_left === null ? "No limit" : duration(stream.seconds_left);
    var total = stream.expires === null ? 0 : stream.expires - stream.started;
    $("ttlbar").style.width = total > 0 ? (100 * stream.seconds_left / total) + "%" : "100%";
    $("viewers").textContent = String(stream.devices.filter(function (d) {
      return d.passes && d.last_seen && info.now - d.last_seen < ACTIVE_SECONDS;
    }).length);
    $("port").textContent = info.stream_port + " " + info.stream_port_state;
    renderVideos(stream);
  }

  // --- devices

  function shortAgent(agent) {
    if (!agent) { return "unknown player"; }
    var known = /(JioSphere|VLC|mpv|Chrome|Firefox|Safari|Kodi|Lavf)[^ ]*/.exec(agent);
    return known ? known[0] : agent.slice(0, 40);
  }

  function deviceState(d) {
    if (d.allowed === null) { return d.passes ? ["Allowed", "ok"] : ["Waiting", "warn"]; }
    return d.allowed ? ["Let in", "ok"] : ["Shut out", "bad"];
  }

  function decide(d, allowed) {
    change("devices", { ip: d.ip, allowed: allowed }, null,
      (allowed ? "Let in " : "Shut out ") + d.ip);
  }

  function renderDevices(info) {
    var stream = info.stream;
    var devices = stream ? stream.devices : [];
    var waiting = devices.filter(function (d) { return d.allowed === null && !d.passes && d.code; });
    var policy = stream ? stream.policy : "";
    segment("policies", policy);
    var lock = $("policies").querySelector('[data-policy="locked"]');
    lock.textContent = policy === "locked" ? "Unlock" : "Locked";
    $("policytext").textContent = stream ? POLICY_TEXT[policy] || "" : "No stream is running.";
    $("waitbadge").hidden = !waiting.length;
    $("waitbadge").textContent = String(waiting.length);
    $("waitcard").hidden = !waiting.length;
    fill($("waiters"), waiting.map(function (d) {
      return h("div", { className: "item device waiting" }, [
        h("div", { className: "grow" }, [
          h("div", { className: "code" }, d.code),
          h("div", { className: "sub" }, d.ip + " · " + shortAgent(d.user_agent))
        ]),
        h("div", { className: "actions" }, [
          button("Deny", function () { decide(d, false); }, "danger"),
          button("Let in", function () { decide(d, true); }, "primary")
        ])
      ]);
    }));
    $("devicecount").textContent = devices.length ? String(devices.length) : "";
    var others = devices.filter(function (d) { return waiting.indexOf(d) < 0; });
    if (!stream) { fill($("devices"), [empty("No stream", "Devices show up here once a stream runs.")]); return; }
    if (!others.length) {
      fill($("devices"), [empty("Nobody yet", "Devices show up here when they open the address.")]);
      return;
    }
    fill($("devices"), others.map(function (d) {
      var state = deviceState(d);
      var ago = info.now - d.last_seen;
      var seen = !d.last_seen ? "never" : ago < ACTIVE_SECONDS ? "watching now" : duration(ago) + " ago";
      var actions = [];
      if (d.allowed !== true) { actions.push(button(policy === "approval" ? "Let in" : "Enable", function () { decide(d, true); })); }
      if (d.allowed !== false) { actions.push(button(policy === "approval" ? "Deny" : "Disable", function () { decide(d, false); }, "danger")); }
      return h("div", { className: "item device" }, [
        h("div", { className: "grow" }, [
          h("div", { className: "row" }, [h("span", { className: "title mono" }, d.ip), pill(state[0], state[1]),
            d.wl ? pill("WL", "warn") : null]),
          h("div", { className: "meta" }, [
            h("span", {}, shortAgent(d.user_agent)), h("span", {}, size(d.bytes) + " sent"),
            h("span", {}, d.requests + " requests" + (d.blocked ? ", " + d.blocked + " blocked" : "")),
            h("span", {}, seen)])
        ]),
        h("div", { className: "actions" }, actions)
      ]);
    }));
  }

  // --- prepares and the cache

  function renderJobs(jobs) {
    var active = jobs.filter(function (j) { return j.state === "queued" || j.state === "running"; });
    var finished = jobs.filter(function (j) { return active.indexOf(j) < 0; });
    var recent = allJobs ? finished : finished.slice(0, RECENT);
    $("jobbadge").hidden = !active.length;
    $("jobbadge").textContent = String(active.length);
    if (!jobs.length) {
      fill($("jobs"), [empty("Nothing to do", "What you prepare shows up here with its progress.")]);
      return;
    }
    var more = finished.length > RECENT ? button(allJobs ? "Show fewer" : "Show " +
      (finished.length - RECENT) + " more", function () { allJobs = !allJobs; renderJobs(jobs); }, "ghost wide") : null;
    fill($("jobs"), active.concat(recent).map(function (job) {
      var kind = { running: "accent", queued: "", done: "ok", failed: "bad", cancelled: "" }[job.state];
      var running = job.state === "running";
      var cancel = running || job.state === "queued" ?
        button("Cancel", function () { change("prepare/cancel", { path: job.source }, null, "Cancelled"); }, "danger") : null;
      return h("div", { className: "item job", style: "grid-template-columns:1fr auto" }, [
        h("div", { className: "grow" }, [
          h("div", { className: "title ellipsis" }, job.name),
          h("div", { className: "sub" }, job.task + " · " + jobState(job)),
          running ? bar(job.progress) : null
        ]),
        h("div", { className: "actions" }, [pill(job.state, kind), cancel])
      ]);
    }).concat([more]));
  }

  function renderCache(list) {
    var total = list.reduce(function (sum, e) { return sum + (e.bytes || 0); }, 0);
    $("cachetotal").textContent = list.length ? size(total) : "";
    if (!list.length) { fill($("cache"), [empty("Nothing is prepared", "Prepared videos stay until you remove them.")]); return; }
    fill($("cache"), list.map(function (e) {
      var name = (e.source || e.key).split("/").pop();
      var chips = [];
      chips.push(e.base ? pill("base " + e.base, "ok") : pill("no base", "bad"));
      if (e.soft.length) { chips.push(pill("soft " + e.soft.join(","), "accent")); }
      if (e.burn.length) { chips.push(pill("burned in " + e.burn.join(","), "accent")); }
      if (e.stale) { chips.push(pill("stale", "warn")); }
      if (e.unchecked) { chips.push(pill("cannot check", "")); }
      if (e.in_use) { chips.push(pill("in use", "ok")); }
      if (e.busy) { chips.push(pill("preparing", "warn")); }
      var rm = e.in_use || e.busy ? null : button("Remove", function () {
        ask("Remove " + name + "?", "Its prepared versions (" + size(e.bytes) + ") are deleted. The video itself stays.",
          "Remove", function () { change("prepared/rm", { path: e.source }, loadCache, "Removed " + name); }, true);
      }, "danger");
      return h("div", { className: "item", style: "grid-template-columns:1fr auto" }, [
        h("div", { className: "grow" }, [
          h("div", { className: "title ellipsis" }, name),
          h("div", { className: "sub mono ellipsis" }, e.source),
          h("div", { className: "chips", style: "margin-top:8px" }, chips)
        ]),
        h("div", { className: "actions" }, [h("span", { className: "muted small" }, size(e.bytes)), rm])
      ]);
    }));
  }

  function loadCache() {
    cacheAt = Date.now();
    request("GET", "prepared").then(function (data) { renderCache(data.prepared); })
      .catch(function (error) { toast(error.message, true); });
  }

  var planned = null;

  function renderPlan(plans) {
    fill($("plans"), plans.map(function (p) {
      var about = p.unknown ? "time unknown" : "about " + duration(p.seconds) + " for a " + duration(p.duration) + " video";
      var tasks = p.tasks.length ? p.tasks.map(function (t) {
        return h("div", { className: "row small" }, [h("span", { className: "grow" }, t.task),
          h("span", { className: "muted" }, duration(t.seconds))]);
      }) : [h("div", { className: "muted small" }, "Nothing to make: it is prepared already.")];
      var tracks = p.tracks.map(function (t) {
        var kind = t.treatment === "soft" ? "accent" : t.treatment === "burned in" ? "warn" : "";
        return pill((t.kind === "audio" ? "A" : "S") + t.number + " " + t.label + (t.kind === "subtitle" ? " · " + t.treatment : ""), kind);
      });
      return h("div", { className: "item", style: "grid-template-columns:1fr" }, [
        h("div", { className: "row" }, [h("span", { className: "title grow ellipsis" }, p.name),
          pill(about, p.longer_than_film || p.unknown ? "warn" : "ok")]),
        h("div", { className: "stack", style: "gap:4px" }, tasks),
        p.notes.length ? h("div", { className: "notice small" }, p.notes.join(" · ")) : null,
        tracks.length ? h("div", { className: "chips" }, tracks) : null
      ]);
    }));
    var work = plans.some(function (p) { return p.tasks.length; });
    $("queue").disabled = !work;
  }

  // --- everything

  function render(info) {
    last = info;
    $("version").textContent = info.version ? "v" + info.version : "";
    $("dot").className = "dot on";
    $("livetext").textContent = info.stream ? "streaming" : "idle";
    var notes = [];
    if (info.firewall_problem) { notes.push(h("div", { className: "notice bad" }, "Firewall: " + info.firewall_problem)); }
    if (info.stream && info.stream.guessers.length) {
      notes.push(h("div", { className: "notice" }, "Shut out for guessing addresses: " +
        info.stream.guessers.join(", ") + ". Enable one on the Devices tab to let it back in."));
    }
    fill($("notes"), notes);
    renderStream(info);
    renderDevices(info);
    renderJobs(info.prepare || []);
  }

  function refresh() {
    if (busy || document.hidden) { return; }
    request("GET", "status").then(render).catch(function () {
      $("dot").className = "dot off";
      $("livetext").textContent = "the daemon does not answer";
    });
    if (tab === "prepare" && Date.now() - cacheAt > CACHE_MS) { loadCache(); }
  }

  function show(name) {
    tab = name;
    Array.prototype.forEach.call(document.querySelectorAll("[data-panel]"), function (p) {
      p.hidden = p.getAttribute("data-panel") !== name;
    });
    Array.prototype.forEach.call($("tabs").querySelectorAll("button"), function (b) {
      b.className = b.getAttribute("data-tab") === name ? "on" : "";
    });
    try { window.localStorage.setItem("vpstream-tab", name); } catch (e) { /* private mode */ }
    if (name === "prepare") { loadCache(); }
  }

  // --- wiring

  $("tabs").addEventListener("click", function (event) {
    var b = event.target.closest("button");
    if (b) { show(b.getAttribute("data-tab")); }
  });
  segmented("kind", function (value) {
    $("path").placeholder = value === "dir" ? "/srv/media/a-show" : "/srv/media/film.mkv";
  });
  segmented("pkind", function (value) {
    $("ppath").placeholder = value === "dir" ? "/srv/media/a-show" : "/srv/media/film.mkv";
  });
  function showBrowserFields() {
    Array.prototype.forEach.call(document.querySelectorAll("label.browser"), function (l) {
      l.hidden = segmentValue("mode") !== "browser";
    });
  }
  segmented("mode", showBrowserFields);
  showBrowserFields();

  $("start").addEventListener("submit", function (event) {
    event.preventDefault();
    // An empty field is the default time limit, never "no limit".
    var ttl = $("ttl").value.trim();
    var options = { ttl: ttl === "" ? null : Number(ttl), mode: segmentValue("mode") };
    options[segmentValue("kind")] = $("path").value.trim();
    ["alias", "policy", "audio", "sub"].forEach(function (name) {
      var value = $(name).value.trim();
      if (value && (options.mode === "browser" || (name !== "audio" && name !== "sub"))) {
        options[name] = value;
      }
    });
    change("stream/start", options, null, "The stream is running");
  });
  $("stop").addEventListener("click", function () {
    ask("Stop the stream?", "Everyone watching is cut off and the stream port is closed.", "Stop",
      function () { change("stream/stop", {}, null, "Stopped"); }, true);
  });
  $("copyurl").addEventListener("click", function () { if (last && last.stream) { copy(last.stream.url); } });
  $("setalias").addEventListener("click", function () {
    var alias = $("newalias").value.trim();
    if (!alias) { $("newalias").focus(); return; }
    change("stream/alias", { alias: alias }, function () { $("newalias").value = ""; }, "Renamed to /" + alias);
  });
  $("policies").addEventListener("click", function (event) {
    var b = event.target.closest("button");
    if (!b || !last || !last.stream) { return; }
    var policy = b.getAttribute("data-policy");
    if (policy === "locked" && last.stream.policy === "locked") { change("stream/unlock", {}, null, "Unlocked"); }
    else if (policy !== last.stream.policy) { change("stream/policy", { policy: policy }, null, "Now " + policy); }
  });

  // A prepare is shown first: what it makes of each video, how long that takes, and the
  // tracks it found; "Prepare it" then queues that plan.
  $("prepareform").addEventListener("submit", function (event) {
    event.preventDefault();
    var options = { dry_run: true };
    options[segmentValue("pkind")] = $("ppath").value.trim();
    if ($("psub").value.trim()) { options.sub = $("psub").value.trim(); }
    change("prepare", options, function (info) {
      delete options.dry_run;
      planned = options;
      renderPlan(info.plans || []);
      $("plan").hidden = false;
      $("plan").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
  document.querySelector("#prepareform .chips").addEventListener("click", function (event) {
    var b = event.target.closest("button");
    if (b) { $("psub").value = b.getAttribute("data-sub"); }
  });
  $("queue").addEventListener("click", function () {
    if (!planned) { return; }
    var options = planned;
    options.confirmed = true;  // the plan, with its time, was shown above
    planned = null;
    $("plan").hidden = true;
    change("prepare", options, loadCache, "Queued");
  });
  $("dropplan").addEventListener("click", function () { planned = null; $("plan").hidden = true; });
  $("rmstale").addEventListener("click", function () {
    change("prepared/rm", { stale: true }, function (info) { loadCache(); toast("Removed " + info.removed); });
  });

  var saved = "stream";
  try { saved = window.localStorage.getItem("vpstream-tab") || "stream"; } catch (e) { /* private mode */ }
  show(saved);
  refresh();
  window.setInterval(refresh, REFRESH_MS);
})();
