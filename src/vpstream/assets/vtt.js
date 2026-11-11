// WebVTT for vpstream's own subtitle overlay: parse a file into cues, and find the text to
// show at a time. Shared by the player and the capability page. ES5, for TV browsers.
(function (root) {
  "use strict";
  var TIME = /^(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})$/;

  function seconds(text) {
    var m = TIME.exec(String(text).trim());
    if (!m) { return NaN; }
    var ms = (m[4] + "00").slice(0, 3);
    return (+(m[1] || 0)) * 3600 + (+m[2]) * 60 + (+m[3]) + (+ms) / 1000;
  }

  // Cue text without WebVTT markup (<i>, <c.yellow>, <00:01.000>) and with its entities.
  function plain(text) {
    return text.replace(/<[^>]*>/g, "")
      .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&nbsp;/g, " ")
      .replace(/&amp;/g, "&");
  }

  function parse(text) {
    var cues = [];
    var lines = String(text).replace(/^﻿/, "").replace(/\r\n?/g, "\n").split("\n");
    for (var i = 0; i < lines.length; i++) {
      var arrow = lines[i].indexOf("-->");
      if (arrow < 0) { continue; }
      var start = seconds(lines[i].slice(0, arrow));
      var end = seconds(lines[i].slice(arrow + 3).trim().split(/\s+/)[0]);
      var body = [];
      for (i++; i < lines.length && lines[i].trim() !== ""; i++) { body.push(plain(lines[i])); }
      if (!isNaN(start) && !isNaN(end) && end > start) {
        cues.push({ start: start, end: end, text: body.join("\n") });
      }
    }
    cues.sort(function (a, b) { return a.start - b.start; });
    return cues;
  }

  // Every cue on screen at time t, in order, one per line.
  function at(cues, t) {
    var shown = [];
    for (var i = 0; i < cues.length && cues[i].start <= t; i++) {
      if (t < cues[i].end && cues[i].text) { shown.push(cues[i].text); }
    }
    return shown.join("\n");
  }

  root.VpVtt = { parse: parse, at: at, seconds: seconds };
})(typeof window !== "undefined" ? window : this);
