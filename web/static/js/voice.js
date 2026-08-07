/* Voice for the assistant: dictation in, spoken replies out.
 *
 * Lives here rather than inline in chat_widget.html because that file's beats are held
 * verbatim in core/record_demo.py -- every edit there raises the re-record surface. This
 * file talks to the widget only through window.SBChat's small seam
 * {open, close, ask, setInput, on}, so neither one has to know much about the other.
 *
 * ── THE APPROVAL GATE IS NOT NEGOTIABLE ────────────────────────────────────────────
 * web/routes/agent.py executes THE STORED PROPOSAL, never what the browser names, and
 * that is what makes the confirmation step real instead of decorative. A spoken shortcut
 * would reintroduce exactly the bypass that design prevents -- over a channel that is
 * lossy, cloud-mediated, and can hear "yes" in a room where nobody said it.
 *
 * So this file does NO keyword matching. Not on "yes", not on "confirm", not on "do it".
 * None. When a reply carries a proposal, voice mode says so, STOPS LISTENING, and puts
 * the focus on the Confirm button. A misheard word must not be able to freeze a card.
 *
 * ── HONEST DEGRADATION ─────────────────────────────────────────────────────────────
 * Speech recognition is a cloud service in every browser that ships it, so it is the
 * first thing to die when the wifi does -- which, for this demo, is a scheduled event.
 * Speech synthesis is local and survives. The mic therefore disables itself with a calm
 * inline note (not a red toast), typing is never affected, and spoken replies keep going.
 */
(function (global) {
  "use strict";

  const SR = global.SpeechRecognition || global.webkitSpeechRecognition;
  const TTS = global.speechSynthesis;

  document.addEventListener("DOMContentLoaded", () => {
    const mic   = document.getElementById("chat-mic");
    const voice = document.getElementById("chat-voice");
    const input = document.getElementById("chat-input");
    const form  = document.getElementById("chat-form");
    const note  = document.getElementById("chat-voice-note");
    const status = document.getElementById("chat-status");
    if (!mic || !voice || !global.SBChat) return;

    let voiceMode = false;      /* speak replies, and listen again after each one */
    let listening = false;
    let sttDead = false;        /* network error: disabled for the rest of the session */
    let awaitingApproval = false;
    let rec = null;
    let retriedSilence = false;

    /* ------------------------------------------------------------------ ui */
    function say(message, tone) {
      if (!note) return;
      note.textContent = message || "";
      note.className = "tiny " + (tone === "warn" ? "" : "dim") +
                       (message ? "" : " hidden");
      if (tone === "warn") note.style.color = "var(--warn)";
      else note.style.removeProperty("color");
    }

    function paintMic() {
      const off = sttDead || !SR;
      mic.disabled = off;
      mic.setAttribute("aria-pressed", listening ? "true" : "false");
      mic.textContent = listening ? "🔴" : "🎤";
      mic.title = off ? "Speech recognition is not available" :
                  listening ? "Stop listening" : "Dictate";
      let pill = document.getElementById("chat-listening");
      if (listening && !pill && status) {
        pill = document.createElement("span");
        pill.id = "chat-listening";
        pill.className = "pill pill-danger";
        pill.style.marginLeft = ".4rem";
        pill.textContent = "listening";
        status.appendChild(pill);
      } else if (!listening && pill) {
        pill.remove();
      }
    }

    function paintVoice() {
      voice.setAttribute("aria-pressed", voiceMode ? "true" : "false");
      voice.textContent = voiceMode ? "🔊" : "🔇";
      voice.title = voiceMode ? "Voice mode on — replies are spoken" : "Voice mode off";
    }

    /* ----------------------------------------------------------------- tts */
    /* A spoken markdown table is unbearable and takes a minute to get through. Replace it
       with a count and let the reader look at the screen, which is where it already is. */
    function speakable(text) {
      const lines = String(text || "").split("\n");
      const out = [];
      let table = 0;
      const flush = () => {
        if (table > 0) {
          out.push(`…and a table of ${table} row${table === 1 ? "" : "s"} on screen.`);
          table = 0;
        }
      };
      lines.forEach(line => {
        const t = line.trim();
        const isRow = t.startsWith("|") || (t.includes("|") && t.split("|").length > 2);
        const isSep = /^\|?[\s:|-]+\|[\s:|-]*$/.test(t) && t.includes("-");
        if (isRow || isSep) {
          /* The separator proves the row above it was the header, not data -- so take it
             back off the count. "a table of 3 rows" for two transactions is wrong, and
             wrong numbers spoken aloud in a bank are worse than no number at all. */
          if (isSep) table = Math.max(0, table - 1);
          else table += 1;
          return;
        }
        flush();
        out.push(line);
      });
      flush();
      return out.join("\n")
        .replace(/```[\s\S]*?```/g, " code block. ")
        .replace(/[*_`#>]/g, "")
        /* Only supply a full stop where the line did not already end in punctuation,
           otherwise every heading is read as "transactions dot dot". */
        .replace(/\n{2,}/g, m => ". ")
        .replace(/([.:;,!?])\s*\.\s/g, "$1 ")
        .replace(/\s+/g, " ")
        .trim();
    }

    function speak(text, whenDone) {
      if (!TTS || !voiceMode) { if (whenDone) whenDone(); return; }
      TTS.cancel();
      const body = speakable(text);
      if (!body) { if (whenDone) whenDone(); return; }
      const u = new SpeechSynthesisUtterance(body);
      u.rate = 1.02;
      u.onend = u.onerror = () => { if (whenDone) whenDone(); };
      TTS.speak(u);
    }

    function hush() { if (TTS) TTS.cancel(); }

    /* ----------------------------------------------------------------- stt */
    function build() {
      const r = new SR();
      r.continuous = false;
      r.interimResults = true;      /* the user sees what was heard before it is sent */
      r.lang = document.documentElement.lang || "en-GB";

      let finalText = "";

      r.onresult = e => {
        let interim = "";
        finalText = "";
        for (let i = 0; i < e.results.length; i++) {
          const chunk = e.results[i][0].transcript;
          if (e.results[i].isFinal) finalText += chunk;
          else interim += chunk;
        }
        /* Straight into the input, always. Sending something the user never saw is the
           one thing a voice interface must not do in a banking app. */
        global.SBChat.setInput((finalText + interim).trim());
      };

      r.onerror = e => {
        listening = false;
        paintMic();
        switch (e.error) {
          case "network":
            /* THE WIFI-OFF CASE. Calm and inline -- this is expected, not a failure. */
            sttDead = true;
            say("Speech recognition needs an internet connection. Typing still works.");
            break;
          case "not-allowed":
          case "service-not-allowed":
            sttDead = true;
            say("Microphone permission is off for this site. Typing still works.");
            break;
          case "no-speech":
            if (!retriedSilence) {       /* one quiet retry, then stop pestering */
              retriedSilence = true;
              start();
              return;
            }
            say("");
            break;
          case "aborted":
            say("");
            break;
          default:
            say("Speech recognition stopped: " + e.error + ". Typing still works.");
        }
      };

      r.onend = () => {
        listening = false;
        paintMic();
        const text = (input.value || "").trim();
        /* Voice mode sends what it heard; mic mode leaves it in the box for review, which
           is the difference between dictation and a hands-free conversation. */
        if (voiceMode && text && !awaitingApproval) {
          global.SBChat.setInput("");
          global.SBChat.ask(text);
        }
      };
      return r;
    }

    function start() {
      if (!SR || sttDead || listening || awaitingApproval) return;
      rec = rec || build();
      try {
        rec.start();
        listening = true;
        say("");
      } catch (e) {
        /* InvalidStateError: already running. Not an error worth showing anyone. */
        listening = true;
      }
      paintMic();
    }

    function stop() {
      listening = false;
      retriedSilence = false;
      if (rec) { try { rec.abort(); } catch (e) { /* already stopped */ } }
      paintMic();
    }

    /* -------------------------------------------------------------- wiring */
    if (!SR) {
      mic.disabled = true;
      mic.title = "This browser has no speech recognition";
      say("Dictation needs Chrome or Edge. Typing works everywhere, and spoken replies " +
          "still work here.");
    }
    paintMic();
    paintVoice();

    mic.addEventListener("click", () => {
      hush();
      if (listening) stop(); else { retriedSilence = false; start(); }
    });

    voice.addEventListener("click", () => {
      voiceMode = !voiceMode;
      paintVoice();
      if (!voiceMode) { hush(); stop(); }
      else say(SR ? "" : "Replies will be spoken. Dictation is not available in this browser.");
    });

    /* Anything the user does by hand outranks the assistant talking over them. */
    input.addEventListener("input", hush);
    document.addEventListener("click", hush, true);
    document.addEventListener("keydown", e => { if (e.key === "Escape") { hush(); stop(); } });

    global.SBChat.on("busy", () => { stop(); hush(); });

    global.SBChat.on("reply", data => {
      const proposal = (data.tool_calls || []).some(
        c => c.requires_approval && c.approved === null);

      if (proposal) {
        /* Stop, and do not restart. The customer confirms on screen or not at all. */
        awaitingApproval = true;
        stop();
        say("Waiting for you to confirm on screen — a spoken yes will not do it.", "warn");
        speak((data.text || "") +
              " You'll need to confirm this on screen — I won't act on a spoken yes.");
        /* Focus the Confirm button so the keyboard path is one key, and watch for either
           button so listening can resume afterwards. */
        setTimeout(() => {
          const card = document.querySelector("#chat-log [data-approve]");
          if (card) {
            card.focus();
            const done = () => {
              awaitingApproval = false;
              say("");
              if (voiceMode) setTimeout(start, 400);
            };
            card.addEventListener("click", done, { once: true });
            const cancel = card.parentElement &&
                           card.parentElement.querySelector("[data-cancel]");
            if (cancel) cancel.addEventListener("click", done, { once: true });
          } else {
            awaitingApproval = false;
          }
        }, 60);
        return;
      }

      speak(data.text, () => {
        if (voiceMode && !awaitingApproval && !sttDead) setTimeout(start, 250);
      });
    });

    global.SBVoice = { speakable, speak, start, stop,
                       get voiceMode() { return voiceMode; },
                       get available() { return !!SR && !sttDead; } };
  });
})(window);
