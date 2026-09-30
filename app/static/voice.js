// Voice input for the text capture form (AC-19). The browser's SpeechRecognition turns speech
// into text in the textarea; only that text is posted with the form, never audio.
// The mic button ships hidden and is shown only where recognition is supported.
// Safe to load more than once (the capture panel is swapped in by htmx).
(function () {
  "use strict";
  if (window.inventoryVoice) {
    window.inventoryVoice.setup(document);
    return;
  }

  var Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;

  function setStatus(button, text) {
    var status = document.querySelector(button.getAttribute("data-voice-status") || "");
    if (status) status.textContent = text;
  }

  function start(button, textarea) {
    var recognition = new Recognition();
    var base = textarea.value.trim();
    var failed = false;
    if (base) base += " ";
    recognition.lang = document.documentElement.lang || navigator.language || "en";
    recognition.interimResults = true;
    recognition.continuous = false;
    recognition.maxAlternatives = 1;

    recognition.onresult = function (event) {
      var finalText = "";
      var interim = "";
      for (var i = 0; i < event.results.length; i++) {
        var result = event.results[i];
        if (result.isFinal) finalText += result[0].transcript;
        else interim += result[0].transcript;
      }
      textarea.value = base + finalText + interim;
    };
    recognition.onerror = function (event) {
      failed = true;
      setStatus(button, event.error === "not-allowed" ? "Microphone access was denied." : "Could not hear that, try again.");
    };
    recognition.onend = function () {
      button._recognition = null;
      button.setAttribute("aria-pressed", "false");
      button.classList.remove("listening");
      if (!failed) setStatus(button, "");
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    };

    button._recognition = recognition;
    button.setAttribute("aria-pressed", "true");
    button.classList.add("listening");
    setStatus(button, "Listening…");
    recognition.start();
  }

  function toggle(button) {
    var textarea = document.querySelector(button.getAttribute("data-voice-target"));
    if (!textarea || textarea.disabled) return;
    if (button._recognition) {
      button._recognition.stop();
      return;
    }
    try {
      start(button, textarea);
    } catch (e) {
      button._recognition = null;
      setStatus(button, "Voice input is not available right now.");
    }
  }

  function setup(root) {
    var buttons = (root.querySelectorAll ? root : document).querySelectorAll("[data-voice-target]");
    for (var i = 0; i < buttons.length; i++) {
      var button = buttons[i];
      if (button.getAttribute("data-voice-ready")) continue;
      button.setAttribute("data-voice-ready", "1");
      if (!Recognition) {
        button.hidden = true;
        continue;
      }
      button.hidden = false;
      button.addEventListener("click", function (event) { toggle(event.currentTarget); });
    }
  }

  window.inventoryVoice = { setup: setup };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { setup(document); });
  } else {
    setup(document);
  }
  document.addEventListener("htmx:load", function (event) { setup(event.detail.elt || document); });
})();
