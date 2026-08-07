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
 * Speech synthesis is local and survives. The mic therefore falls back with a calm inline
 * note (not a red toast), typing is never affected, and spoken replies keep going.
 *
 * A network failure stops the AUTO-RESTART LOOP but does not disable the button. Those
 * were one flag once, and it was wrong: Chrome throws a spurious `network` at cold start
 * often enough that a single one cannot be treated as permanent, and latching the button
 * off meant the only way back was a page reload -- during a demo, on the one control the
 * user is already looking at. The loop must stop (a dead service would spin forever); the
 * user must still be able to click. Only a browser with no API at all disables the button.
 *
 * The wording matters too. `network` does not prove the wifi is down -- it is equally a
 * proxy refusing the speech endpoint on a connection that is otherwise fine, which is the
 * likelier reading on a corporate network. Ask navigator.onLine before blaming the wifi,
 * and send anyone who wants the real answer to /static/_voice_check.html.
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
    let sttDead = false;        /* stop AUTO-restarting; a manual click still retries */
    let sttBlocked = false;     /* no API in this browser: the button is genuinely useless */
    let netFails = 0;           /* consecutive `network` errors, reset by any audio */
    let awaitingApproval = false;
    let rec = null;
    let retriedSilence = false;

    /* navigator.onLine is only trustworthy in one direction -- false really does mean no
       route, true means "a network exists", not "the internet answers". That asymmetry is
       exactly what is needed here: it is enough to stop us blaming the wifi wrongly. */
    function offline() { return "onLine" in navigator && !navigator.onLine; }

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
      /* Only a browser with no API disables the button. After a failure the mic stays
         clickable on purpose -- retrying is one tap, and the alternative is a reload. */
      mic.disabled = sttBlocked;
      mic.setAttribute("aria-pressed", listening ? "true" : "false");
      mic.textContent = listening ? "🔴" : "🎤";
      mic.title = sttBlocked ? "Speech recognition is not available in this browser" :
                  sttDead ? "Dictation failed — click to try again" :
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

      /* Audio is flowing, so whatever failed last time is over. Clear the failure state
         here rather than in onresult -- the service is proven up the moment it opens the
         stream, and waiting for a transcript would leave the warning on screen through a
         silent pause. */
      r.onaudiostart = () => { netFails = 0; sttDead = false; say(""); paintMic(); };

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
        switch (e.error) {
          case "network":
            /* Stop the auto-restart loop, keep the button live. Calm and inline -- with
               the wifi off this is expected, not a failure. */
            sttDead = true;
            netFails += 1;
            if (netFails === 1) {
              say("Couldn't reach the speech service. Click the mic to try again — " +
                  "typing always works.");
            } else if (offline()) {
              say("Speech recognition needs an internet connection: dictation is the one " +
                  "part of this that cannot work offline. Typing and spoken replies still " +
                  "work.");
            } else {
              /* Online, and still refused. Blaming the wifi here would send someone to
                 fix the wrong thing -- on this network the likelier answer is a proxy. */
              say("The speech service is unreachable even though the connection is up — " +
                  "usually a proxy or the browser. Diagnose it at /static/_voice_check.html. " +
                  "Typing still works.", "warn");
            }
            break;
          case "not-allowed":
          case "service-not-allowed":
            sttDead = true;
            say("Microphone permission is off for this site. Allow it from the padlock in " +
                "the address bar, then click the mic again. Typing still works.");
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
        /* After the switch, not before it -- the button's tooltip is the affordance that
           says a retry is possible, and painting it first showed the pre-failure state. */
        paintMic();
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

    /* `manual` = the user clicked the mic. That clears a previous failure and tries again;
       the automatic restart after a spoken reply does not, or a dead service would spin. */
    function start(manual) {
      if (!SR || sttBlocked || listening || awaitingApproval) return;
      if (sttDead && !manual) return;
      if (manual) sttDead = false;
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
      sttBlocked = true;
      say("Dictation needs Chrome or Edge. Typing works everywhere, and spoken replies " +
          "still work here.");
    }
    paintMic();
    paintVoice();

    mic.addEventListener("click", () => {
      hush();
      if (listening) stop(); else { retriedSilence = false; start(true); }
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
              if (voiceMode) setTimeout(() => start(), 400);
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

      /* No sttDead check here -- start() owns that decision, and a second copy of the rule
         is a second place for it to drift. */
      speak(data.text, () => {
        if (voiceMode && !awaitingApproval) setTimeout(() => start(), 250);
      });
    });

    global.SBVoice = { speakable, speak, start, stop,
                       get voiceMode() { return voiceMode; },
                       get available() { return !!SR && !sttBlocked; },
                       get healthy() { return !!SR && !sttBlocked && !sttDead; } };
  });
})(window);
