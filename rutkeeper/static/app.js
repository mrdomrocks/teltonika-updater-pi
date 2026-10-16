(function () {
  var clock = document.querySelector(".clock");
  var face = document.getElementById("clock");
  if (clock && face && clock.dataset.epoch) {
    var started = Date.now();
    var base = Number(clock.dataset.epoch);
    window.setInterval(function () {
      var now = new Date(base + (Date.now() - started));
      var hours = String(now.getHours()).padStart(2, "0");
      var minutes = String(now.getMinutes()).padStart(2, "0");
      face.textContent = hours + ":" + minutes;
    }, 15000);
  }

  if (window.location.search.indexOf("notice=") !== -1 && window.history.replaceState) {
    var url = new URL(window.location.href);
    url.searchParams.delete("notice");
    var next = url.pathname + (url.searchParams.toString() ? "?" + url.searchParams.toString() : "") + url.hash;
    window.history.replaceState({}, "", next);
  }

  var hero = document.getElementById("bench-hero");
  if (hero) {
    var lastTick = hero.getAttribute("data-ready-tick") || "0";
    var primed = false;
    var notifyButton = document.getElementById("bench-notify");
    if (notifyButton && window.Notification) {
      notifyButton.addEventListener("click", function () {
        Notification.requestPermission();
      });
    } else if (notifyButton) {
      notifyButton.hidden = true;
    }

    var beep = function () {
      try {
        var context = new (window.AudioContext || window.webkitAudioContext)();
        var oscillator = context.createOscillator();
        var gain = context.createGain();
        oscillator.frequency.value = 880;
        gain.gain.value = 0.04;
        oscillator.connect(gain);
        gain.connect(context.destination);
        oscillator.start();
        oscillator.stop(context.currentTime + 0.18);
      } catch (error) {
        return error;
      }
    };

    var steps = ["login", "firmware", "firmware-reboot", "config", "config-reboot", "password"];
    var last = null;

    var paint = function (data) {
      last = data;
      hero.setAttribute("data-phase", data.phase || "offline");
      var kicker = document.getElementById("bench-kicker");
      var detail = document.getElementById("bench-detail");
      var rtt = document.getElementById("bench-rtt");
      var host = document.getElementById("bench-host");
      var busy = data.phase === "busy" || data.phase === "rebooting";
      if (kicker) {
        if (data.phase === "rebooting") kicker.textContent = "Rebooting";
        else if (busy) kicker.textContent = "Installing";
        else if (data.http) kicker.textContent = "Ready";
        else if (data.ping) kicker.textContent = "Ping only";
        else kicker.textContent = "Waiting";
      }
      if (detail) detail.textContent = data.error || data.detail || "";
      if (host) host.textContent = data.host || "";
      if (rtt) {
        rtt.textContent = data.rtt_ms ? "Last ping " + data.rtt_ms + " ms" : data.ping ? "Ping answered" : "No ping yet";
      }
      var current = data.step || "";
      var index = steps.indexOf(current);
      var failed = Boolean(data.error);
      document.querySelectorAll("#install-steps li").forEach(function (item) {
        var mine = steps.indexOf(item.getAttribute("data-step"));
        item.classList.remove("done", "now", "fail");
        if (current === "done" || (index > mine && mine >= 0)) item.classList.add("done");
        else if (index === mine) item.classList.add(failed ? "fail" : "now");
      });
      var radio1 = document.querySelector('#bench-run input[value="config1"]');
      var radio2 = document.querySelector('#bench-run input[value="config2"]');
      if (radio1) {
        radio1.disabled = busy || !data.config1_ready;
        if (radio1.disabled) radio1.checked = false;
      }
      if (radio2) {
        radio2.disabled = busy || !data.config2_ready;
        if (radio2.disabled) radio2.checked = false;
      }
      var selected = document.querySelector('#bench-run input[name="config"]:checked');
      var slot = selected ? selected.value : "";
      var configReady = slot === "config1" ? data.config1_ready : slot === "config2" ? data.config2_ready : false;
      var password = document.querySelector('#bench-run input[name="current_password"]');
      var typed = Boolean(password && password.value);
      var install = document.getElementById("install-button");
      if (install) {
        install.disabled = busy || !typed || !data.http || !data.firmware_ready || !configReady || !data.has_target_password;
      }
      var hint = document.getElementById("install-hint");
      if (hint && !busy && !data.error) {
        if (!data.firmware_ready) hint.textContent = "Store a firmware file first.";
        else if (!data.config1_ready && !data.config2_ready) hint.textContent = "Store Config 1 or Config 2.";
        else if (!data.has_target_password) hint.textContent = "Save the shared password first.";
        else if (!data.http) hint.textContent = "Waiting for the router to answer.";
        else if (!typed) hint.textContent = "Type this router’s current password, pick a config, then install.";
        else if (!configReady) hint.textContent = "Pick Config 1 or Config 2.";
        else hint.textContent = "Install signs in, writes the firmware, applies the config, then sets the shared password.";
      }
    };

    var passwordField = document.querySelector('#bench-run input[name="current_password"]');
    if (passwordField) {
      passwordField.addEventListener("input", function () {
        if (last) paint(last);
      });
    }
    document.querySelectorAll('#bench-run input[name="config"]').forEach(function (radio) {
      radio.addEventListener("change", function () {
        if (last) paint(last);
      });
    });

    var poll = function () {
      fetch("/bench/status", { headers: { Accept: "application/json" } })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          paint(data);
          var tick = String(data.ready_tick || 0);
          if (!primed) {
            primed = true;
            lastTick = tick;
            return;
          }
          if (tick !== lastTick && data.http) {
            lastTick = tick;
            var announce = document.getElementById("bench-announce");
            var message = data.host + " answered. The router is ready.";
            if (announce) {
              announce.hidden = false;
              announce.textContent = message;
            }
            beep();
            if (window.Notification && Notification.permission === "granted") {
              new Notification("TUA", { body: message });
            }
          } else {
            lastTick = tick;
          }
        })
        .catch(function () { return null; });
    };
    poll();
    window.setInterval(poll, 1000);
  }

  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (form.id === "bench-run") {
        var install = document.getElementById("install-button");
        if (install && install.disabled) {
          event.preventDefault();
          return;
        }
      }
      var message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) {
        event.preventDefault();
        return;
      }
      window.setTimeout(function () {
        if (event.defaultPrevented) {
          return;
        }
        form.querySelectorAll("button[type=submit]").forEach(function (button) {
          if (!button.disabled) {
            button.textContent = "Working…";
            button.disabled = true;
          }
        });
      }, 0);
    });
  });
})();
