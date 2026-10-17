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

    var steps = ["login", "ready", "password", "firmware", "firmware-reboot", "new-address", "config"];
    var last = null;

    var paint = function (data) {
      last = data;
      hero.setAttribute("data-phase", data.phase || "offline");
      var kicker = document.getElementById("bench-kicker");
      var detail = document.getElementById("bench-detail");
      var rtt = document.getElementById("bench-rtt");
      var host = document.getElementById("bench-host");
      var busy = data.phase === "busy" || data.phase === "rebooting" || data.phase === "await-address";
      if (kicker) {
        if (data.phase === "await-address") kicker.textContent = "New address";
        else if (data.phase === "rebooting") kicker.textContent = "Rebooting";
        else if (busy) kicker.textContent = "Installing";
        else if (data.http) kicker.textContent = "Ready";
        else if (data.ping) kicker.textContent = "Ping only";
        else kicker.textContent = "Waiting";
      }
      if (detail) detail.textContent = data.error || data.detail || "";
      if (host) {
        host.textContent = data.phase === "await-address" ? (data.next_host || "New address") : (data.host || "");
      }
      var nextHost = document.getElementById("next-host");
      var nextLabel = document.getElementById("next-host-label");
      if (nextLabel) nextLabel.classList.toggle("attention", data.phase === "await-address");
      if (nextHost && data.phase === "await-address" && !nextHost.dataset.shown) {
        nextHost.dataset.shown = "1";
        nextHost.scrollIntoView({ block: "center" });
      }
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
        install.disabled = busy || !typed || !data.http || !data.firmware_ready || !configReady;
      }
      var hint = document.getElementById("install-hint");
      if (hint && data.phase === "await-address") {
        hint.textContent = "Enter the router’s new address and save. The config is applied when that address answers.";
      } else if (hint && !busy && !data.error) {
        if (!data.firmware_ready) hint.textContent = "Put a firmware file in the Firmware folder.";
        else if (!data.config1_ready && !data.config2_ready) hint.textContent = "Put a config file in the Config 1 or Config 2 folder.";
        else if (!data.http) hint.textContent = "Waiting for the router to answer.";
        else if (!typed) hint.textContent = "Type this router’s current password or scan its label, pick a config, then install.";
        else if (!configReady) hint.textContent = "Pick Config 1 or Config 2.";
        else hint.textContent = "Install signs in, sets the admin password, writes the firmware, then applies the config on the new address.";
      }
    };

    var passwordField = document.querySelector('#bench-run input[name="current_password"]');
    if (passwordField) {
      passwordField.addEventListener("input", function () {
        if (last) paint(last);
      });
    }

    var scanStatus = document.getElementById("scan-status");
    var photoInput = document.getElementById("label-photo");
    var scanButton = document.getElementById("scan-label");
    var overlay = document.getElementById("scan-overlay");
    var video = document.getElementById("scan-video");
    var scanStream = null;
    var scanTimer = 0;
    var liveDetector = null;
    var scanCanvas = document.createElement("canvas");
    var scanContext = scanCanvas.getContext("2d", { willReadFrequently: true });

    var setScanStatus = function (text, kind) {
      if (!scanStatus) return;
      scanStatus.textContent = text || "";
      scanStatus.classList.remove("ok", "bad");
      if (kind) scanStatus.classList.add(kind);
    };

    var applyLabel = function (raw) {
      if (!window.TUALabel) return;
      var parsed;
      try {
        parsed = window.TUALabel.parseLabel(raw);
      } catch (error) {
        setScanStatus(window.TUALabel.message(error && error.code), "bad");
        return;
      }
      if (!passwordField || !parsed.password) return;
      passwordField.value = parsed.password;
      passwordField.dispatchEvent(new Event("input", { bubbles: true }));
      var username = document.querySelector('input[name="username"]');
      var note = "Password filled from the label.";
      if (parsed.username && username && username.value !== parsed.username) {
        note += " Username on the label is " + parsed.username + ".";
      }
      setScanStatus(note, "ok");
    };

    var readCode = function (source, width, height, sx, sy, sw, sh) {
      if (!window.jsQR || !scanContext) return "";
      var longEdge = Math.max(sw, sh);
      var scale = Math.min(1, 1800 / longEdge);
      var w = Math.max(1, Math.round(sw * scale));
      var h = Math.max(1, Math.round(sh * scale));
      scanCanvas.width = w;
      scanCanvas.height = h;
      scanContext.drawImage(source, sx, sy, sw, sh, 0, 0, w, h);
      var image = scanContext.getImageData(0, 0, w, h);
      var code = window.jsQR(image.data, w, h, { inversionAttempts: "attemptBoth" });
      return code && code.data ? code.data : "";
    };

    var readBitmap = function (source, width, height) {
      var found = readCode(source, width, height, 0, 0, width, height);
      if (found) return found;
      var crop = 0.62;
      var sw = Math.max(1, Math.round(width * crop));
      var sh = Math.max(1, Math.round(height * crop));
      var sx = Math.round((width - sw) / 2);
      var sy = Math.round((height - sh) / 2);
      return readCode(source, width, height, sx, sy, sw, sh);
    };

    if (photoInput) {
      photoInput.addEventListener("change", function () {
        var file = photoInput.files && photoInput.files[0];
        photoInput.value = "";
        if (!file || !window.TUALabel) return;
        setScanStatus("Reading the label…", "");
        var finish = function (raw) {
          if (!raw) {
            setScanStatus(window.TUALabel.message("no-code"), "bad");
            return;
          }
          applyLabel(raw);
        };
        createImageBitmap(file).then(function (bitmap) {
          var native = window.BarcodeDetector
            ? new BarcodeDetector({ formats: ["qr_code"] }).detect(bitmap).then(function (codes) {
              return codes && codes[0] && codes[0].rawValue ? codes[0].rawValue : "";
            }).catch(function () { return ""; })
            : Promise.resolve("");
          return native.then(function (raw) {
            var found = raw || readBitmap(bitmap, bitmap.width, bitmap.height);
            if (bitmap.close) bitmap.close();
            finish(found);
          });
        }).catch(function () {
          setScanStatus(window.TUALabel.message("unreadable"), "bad");
        });
      });
    }

    var stopLive = function () {
      window.clearInterval(scanTimer);
      scanTimer = 0;
      if (scanStream) {
        scanStream.getTracks().forEach(function (track) { track.stop(); });
        scanStream = null;
      }
      if (video) video.srcObject = null;
      if (overlay) {
        overlay.hidden = true;
        overlay.setAttribute("aria-hidden", "true");
      }
      liveDetector = null;
    };

    var tickLive = function () {
      if (!video || video.readyState < 2) return;
      if (liveDetector) {
        liveDetector.detect(video).then(function (codes) {
          var value = codes && codes[0] && codes[0].rawValue;
          if (!value) return;
          stopLive();
          applyLabel(value);
        }).catch(function () { return null; });
        return;
      }
      var raw = readBitmap(video, video.videoWidth, video.videoHeight);
      if (raw) {
        stopLive();
        applyLabel(raw);
      }
    };

    if (scanButton) {
      scanButton.addEventListener("click", function () {
        var live = window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
        if (!live) {
          if (photoInput) photoInput.click();
          return;
        }
        navigator.mediaDevices.getUserMedia({
          audio: false,
          video: { facingMode: { ideal: "environment" } }
        }).then(function (stream) {
          scanStream = stream;
          if (video) {
            video.srcObject = stream;
            video.play();
          }
          liveDetector = window.BarcodeDetector ? new BarcodeDetector({ formats: ["qr_code"] }) : null;
          if (overlay) {
            overlay.hidden = false;
            overlay.setAttribute("aria-hidden", "false");
          }
          scanTimer = window.setInterval(tickLive, 400);
        }).catch(function () {
          setScanStatus("The camera did not open. Take a photo of the QR code instead.", "bad");
          if (photoInput) photoInput.click();
        });
      });
    }
    var scanCancel = document.getElementById("scan-cancel");
    if (scanCancel) scanCancel.addEventListener("click", stopLive);
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
