(function () {
  var LABEL = { supported: "Supported", contradicted: "Contradicted", outdated: "Outdated", misleading: "Misleading", unverified: "Can't verify" };
  var form = document.getElementById("form"), text = document.getElementById("text");
  var go = document.getElementById("go"), status = document.getElementById("status"), out = document.getElementById("out");

  function el(tag, cls, txt) { var e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; }

  function head(r) {
    var h = el("div", "receipt-head"), c = r.counts;
    if (c && r.claims.length) {
      var fix = c.contradicted + c.outdated + (c.misleading || 0);
      h.appendChild(el("strong", "verdict-line", fix ? "Fix " + fix + (fix === 1 ? " claim" : " claims") + " before you give this talk."
        : c.unverified ? "Nothing wrong found. Have your own data ready for the claims marked can't verify." : "Every claim checks out."));
    }
    h.appendChild(el("span", null, (r.audio_seconds != null ? fmt(r.audio_seconds) + " talk · " : "") + r.claims.length + " claims · " + (r.seconds != null ? r.seconds + " s · " : "") + r.checked_on));
    h.appendChild(el("span", null, c ? [c.contradicted + " contradicted", c.outdated + " outdated", (c.misleading || 0) + " misleading", c.supported + " supported", c.unverified + " can't verify"].join(" · ") : "checking…"));
    return h;
  }

  function render(r) {
    out.textContent = "";
    var box = el("div", "receipt");
    box.appendChild(head(r));
    if (!r.claims.length) box.appendChild(el("div", "empty", "No checkable claims found. Opinions and greetings are skipped on purpose."));
    r.claims.forEach(function (k) { box.appendChild(k.verdict ? row(k) : pendingRow(k)); });
    out.appendChild(box);
    return box;
  }

  function pendingRow(k) {
    var r = el("div", "row pending"), line = el("div", "claimline");
    line.appendChild(el("div", "claim", "“" + k.quote + "”"));
    r.appendChild(line);
    r.appendChild(el("span", "pill pending", "Checking…"));
    return r;
  }

  function row(k) {
    var r = el("div", "row");
    var line = el("div", "claimline");
    if (k.time != null) {
      var ts = el("button", "ts", "▶ " + fmt(k.time)); ts.type = "button"; ts.title = "Hear this moment in your recording";
      if (!player.src) ts.disabled = true;
      ts.addEventListener("click", function () { player.currentTime = Math.max(k.time - 0.3, 0); player.play().catch(function () {}); });
      line.appendChild(ts);
    }
    line.appendChild(el("div", "claim", "“" + k.quote + "”"));
    r.appendChild(line);
    r.appendChild(el("span", "pill " + k.verdict, LABEL[k.verdict] || k.verdict));
    var d = el("div", "detail");
    // What to do first, then why, then the proof.
    if (k.suggestion) { var say = el("span", "say"); say.appendChild(el("b", null, "Say instead")); say.appendChild(document.createTextNode(k.suggestion)); d.appendChild(say); }
    if (k.explanation) d.appendChild(el("span", null, k.explanation));
    if (k.evidence_quote) d.appendChild(el("blockquote", null, k.evidence_quote));
    if (k.source && k.source.url) {
      var a = el("a", null, k.source.title); a.href = k.source.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      var s = el("span", null, "Source: "); s.appendChild(a); d.appendChild(s);
      if (k.source_tier === "open_web") d.appendChild(el("span", "tier", "Found on the open web, outside the reference sources. Check this source yourself."));
    }
    if (k.guard_note) d.appendChild(el("span", "guard", "Guard: " + k.guard_note));
    r.appendChild(d);
    return r;
  }

  function fmt(sec) { sec = Math.round(sec); return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0"); }

  function call(url, opts) {
    return fetch(url, opts).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (j) {
        if (!res.ok) throw new Error(j.detail || (res.status === 504 ? "The check took too long. Try a shorter text." : "The check failed (error " + res.status + "). Try again."));
        return j;
      });
    }, function () {
      throw new Error(location.hostname === "localhost" || location.hostname === "127.0.0.1"
        ? "The local Sayproof server isn't running. Start it, or use sayproof.vercel.app."
        : "Couldn't reach the Sayproof server. Check your connection and try again.");
    });
  }
  // Reads newline-delimited JSON as it arrives and hands each event to onEvent.
  function stream(url, opts, onEvent) {
    return fetch(url, opts).then(function (res) {
      if (!res.ok) return res.json().catch(function () { return {}; }).then(function (j) {
        throw new Error(j.detail || (res.status === 504 ? "The check took too long. Try a shorter text." : "The check failed (error " + res.status + "). Try again."));
      });
      var reader = res.body.getReader(), decoder = new TextDecoder(), buf = "";
      function pump() {
        return reader.read().then(function (chunk) {
          buf += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
          var lines = buf.split("\n"); buf = chunk.done ? "" : lines.pop();
          lines.forEach(function (l) { if (l.trim()) onEvent(JSON.parse(l)); });
          if (!chunk.done) return pump();
        });
      }
      return pump();
    }, function () { return call(url, opts); });
  }
  function busy(on, msg) { go.disabled = on; rec.disabled = on && !recording; file.disabled = on; status.className = "status"; if (msg) status.textContent = msg; }
  function fail(err) { status.className = "status err"; status.textContent = String(err.message || err); }

  // Streams one check of text t into the receipt. A recording's times are added when its transcript is checked.
  function runCheck(t, steps) {
    var started = Date.now(), receipt = null, box = null, done = 0, phase = steps + "Finding the claims in your talk";
    function tick() { status.textContent = phase + (receipt && receipt.seconds != null ? "." : " · " + Math.round((Date.now() - started) / 1000) + " s"); }
    busy(true); tick();
    var clock = setInterval(tick, 1000);
    function onEvent(e) {
      if (e.event === "error") throw new Error(e.detail);
      if (e.event === "claims") {
        receipt = { claims: e.claims, checked_on: e.checked_on, audio_seconds: take ? take.audio_seconds : null };
        receipt.claims.forEach(function (k) { k.time = timeOf(k, t); });
        box = render(receipt);
        phase = e.claims.length ? steps + "Searching sources for " + e.claims.length + " claims" : "No claims to check";
        if (e.more) phase += " (the first " + e.claims.length + "; " + e.more + " more found but not checked, check the rest separately)";
      } else if (e.event === "result") {
        e.claim.time = receipt.claims[e.index].time;
        receipt.claims[e.index] = e.claim;
        box.replaceChild(row(e.claim), box.children[e.index + 1]);
        done += 1; phase = steps + "Checked " + done + " of " + receipt.claims.length + " claims";
      } else if (e.event === "done") {
        receipt.counts = e.counts; receipt.seconds = e.seconds;
        box.replaceChild(head(receipt), box.firstChild);
        phase = "Done in " + e.seconds + " s. Checked with " + e.model
          + (take ? ". Press a ▶ time to hear that moment. Did " + asrName + " mishear a word? Fix it in the text and press Check my talk again" : "");
      }
      tick();
    }
    return stream("/api/check-stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: t }) }, onEvent)
      .then(function () { if (!receipt || receipt.seconds == null) throw new Error("The check stopped before it finished. Try again."); })
      .catch(fail).finally(function () { clearInterval(clock); busy(false); });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var t = text.value.trim();
    if (t.length < 10) { fail("Write at least one full sentence to check."); return; }
    runCheck(t, "");
  });

  // Ctrl/Cmd+Enter in the text box runs the check.
  text.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !go.disabled) { e.preventDefault(); form.requestSubmit(); }
  });

  // ---- speech input (Parakeet on the local server, ElevenLabs when hosted) ----
  var player = document.getElementById("player"), takeBox = document.getElementById("take");
  var rec = document.getElementById("rec"), file = document.getElementById("file");
  var recorder = null, chunks = [], recording = false, timer = null, startedAt = 0;
  var asrName = "Parakeet", viaStorage = false;
  var take = null; // the last transcribed recording: { transcript, audio_seconds, marks: [[char offset, seconds], ...] }

  // Time of a claim in the recording. If the text was edited after transcription, the claim's words are looked up in the original.
  function timeOf(k, t) {
    if (!take || !take.marks.length) return null;
    var pos = t === take.transcript ? k.position : take.transcript.toLowerCase().indexOf(String(k.quote || "").toLowerCase());
    if (pos == null || pos < 0) return null;
    var at = take.marks[0][1];
    for (var i = 0; i < take.marks.length && take.marks[i][0] <= pos; i++) at = take.marks[i][1];
    return Math.round(at * 10) / 10;
  }

  function sendAudio(blob, name) {
    if (player.src) URL.revokeObjectURL(player.src);
    player.src = URL.createObjectURL(blob); takeBox.hidden = false;
    take = null; out.textContent = "";
    var started = Date.now(), msg = "Step 1 of 2 · " + asrName + " is writing down your talk";
    function tick() { status.textContent = msg + " · " + Math.round((Date.now() - started) / 1000) + " s" + (asrName === "Parakeet" && Date.now() - started > 8000 ? " (the first run loads the model and can take a minute)" : ""); }
    busy(true); tick();
    var clock = setInterval(tick, 1000);
    transcribe(blob, name, function (m) { msg = m; tick(); })
      .then(function (r) {
        clearInterval(clock);
        take = r; text.value = r.transcript.slice(0, text.maxLength);
        var cut = r.transcript.length > text.maxLength ? "Only the first 30 minutes are checked · " : "";
        return runCheck(text.value.trim(), "Step 2 of 2 · " + cut);
      })
      .catch(function (err) { clearInterval(clock); busy(false); fail(err); });
  }

  // Hosted: the recording goes straight to private Supabase storage, past the 4.5 MB request limit of the host.
  function transcribe(blob, name, step) {
    if (!viaStorage) {
      var fd = new FormData(); fd.append("audio", blob, name);
      return call("/api/transcribe", { method: "POST", body: fd });
    }
    var json = { "Content-Type": "application/json" };
    step("Step 1 of 2 · Uploading your recording");
    return call("/api/upload-url", { method: "POST", headers: json, body: JSON.stringify({ filename: name, size: blob.size }) })
      .then(function (u) {
        return fetch(u.upload_url, { method: "PUT", headers: { "Content-Type": (blob.type || "audio/webm").split(";")[0] }, body: blob })
          .then(function (res) {
            if (!res.ok) throw new Error("The upload failed (error " + res.status + "). Try again.");
            step("Step 1 of 2 · " + asrName + " is writing down your talk");
            return call("/api/transcribe-stored", { method: "POST", headers: json, body: JSON.stringify({ path: u.path }) });
          });
      });
  }

  rec.addEventListener("click", function () {
    if (recording) { recorder.stop(); return; }
    if (!navigator.mediaDevices || !window.MediaRecorder) { fail("This browser can't record audio. Upload a recording instead."); return; }
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      chunks = []; recorder = new MediaRecorder(stream);
      recorder.ondataavailable = function (e) { if (e.data.size) chunks.push(e.data); };
      recorder.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        recording = false; clearInterval(timer); rec.classList.remove("on"); rec.textContent = "● Record rehearsal";
        if (Date.now() - startedAt < 2000) { fail("That was under 2 seconds. Press Record, give your talk, then press Stop."); return; }
        var type = recorder.mimeType || "audio/webm";
        sendAudio(new Blob(chunks, { type: type }), "rehearsal." + (type.indexOf("mp4") > -1 ? "mp4" : "webm"));
      };
      recorder.start(); recording = true; startedAt = Date.now(); rec.classList.add("on");
      status.className = "status"; status.textContent = "Recording. Speak as you would on stage, then press Stop.";
      rec.textContent = "■ Stop 0:00";
      timer = setInterval(function () { rec.textContent = "■ Stop " + fmt((Date.now() - startedAt) / 1000); }, 500);
    }, function () { fail("Microphone access was blocked. Allow it in the browser's address bar, or upload a recording."); });
  });
  file.addEventListener("change", function () { if (file.files[0]) sendAudio(file.files[0], file.files[0].name); file.value = ""; });

  fetch("/api/health").then(function (r) { return r.json(); }).then(function (h) {
    if (!h.speech) return;
    if (h.asr === "ElevenLabs") {
      asrName = "ElevenLabs"; viaStorage = !!h.storage;
      document.getElementById("asr-where").textContent = "ElevenLabs writes it down";
    }
    ["speech-on", "speech-actions"].forEach(function (id) { document.getElementById(id).hidden = false; });
    document.getElementById("speech-off").hidden = true;
    // Browsers allow the microphone only on https or localhost, so a phone opening http://<computer>:8765 uploads instead.
    if (!window.isSecureContext) {
      rec.hidden = true;
      document.getElementById("speech-on").hidden = true;
      document.getElementById("speech-phone").hidden = false;
      return;
    }
    return call("/api/phone").then(function (q) {
      document.getElementById("phone-qr").innerHTML = q.svg; // SVG built by our own server from its own address
      document.getElementById("phone-url").textContent = q.url;
      document.getElementById("phone").hidden = false;
    });
  }).catch(function () {});

  document.getElementById("clear").addEventListener("click", function () {
    take = null; takeBox.hidden = true; player.pause(); if (player.src) URL.revokeObjectURL(player.src); player.removeAttribute("src");
    text.value = ""; out.textContent = ""; text.focus(); status.className = "status"; status.textContent = ""; });
})();
