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

    var paint = function (data) {
      hero.setAttribute("data-phase", data.phase || "offline");
      var kicker = document.getElementById("bench-kicker");
      var detail = document.getElementById("bench-detail");
      var rtt = document.getElementById("bench-rtt");
      var host = document.getElementById("bench-host");
      if (kicker) {
        kicker.textContent = data.http ? "Ready" : data.ping ? "Ping only" : data.phase === "rebooting" ? "Rebooting" : "Waiting";
      }
      if (detail) detail.textContent = data.error || data.detail || "";
      if (host) host.textContent = data.host || "";
      if (rtt) {
        rtt.textContent = data.rtt_ms ? "Last ping " + data.rtt_ms + " ms" : data.ping ? "Ping answered" : "No ping yet";
      }
      var flash = document.getElementById("flash-button");
      var config1 = document.getElementById("config1-button");
      var config2 = document.getElementById("config2-button");
      var busy = data.phase === "busy" || data.phase === "rebooting";
      if (flash) flash.disabled = busy || !data.firmware_ready || !data.http;
      if (config1) config1.disabled = busy || !data.config1_ready || !data.http;
      if (config2) config2.disabled = busy || !data.config2_ready || !data.http;
    };

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
              new Notification("Rutkeeper", { body: message });
            }
          } else {
            lastTick = tick;
          }
        })
        .catch(function () { return null; });
    };
    poll();
    window.setInterval(poll, 2000);
  }

  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
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
