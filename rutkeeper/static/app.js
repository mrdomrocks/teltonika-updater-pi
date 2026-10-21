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

    var steps = ["login", "ready", "password", "firmware", "firmware-reboot", "config", "new-address"];
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
      var signedIn = current === "signed-in";
      document.querySelectorAll("#install-steps li").forEach(function (item) {
        var mine = steps.indexOf(item.getAttribute("data-step"));
        item.classList.remove("done", "now", "fail");
        if (signedIn) {
          if (item.getAttribute("data-step") === "login") item.classList.add("done");
          return;
        }
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
      var login = document.getElementById("login-button");
      if (login) {
        // Password optional: Sign in also tries Enter@qua1! after a partial install.
        login.disabled = busy;
      }
      var applyConfig = document.getElementById("config-button");
      if (applyConfig) {
        applyConfig.disabled = busy || !typed || !data.http || !configReady;
      }
      var hint = document.getElementById("install-hint");
      if (hint && data.phase === "await-address") {
        hint.textContent = "Waiting for the router on its new address. Answering there confirms the config was uploaded.";
      } else if (hint && signedIn && !data.error) {
        hint.textContent = "Signed in. Install runs the full job, or Apply config if firmware is already on this router.";
      } else if (hint && !busy && !data.error) {
        if (!data.firmware_ready) hint.textContent = "Put a firmware file in the Firmware folder.";
        else if (!data.config1_ready && !data.config2_ready) hint.textContent = "Put a config file in the Config 1 or Config 2 folder.";
        else if (!typed) hint.textContent = "Type this router’s current password or scan its label. Use Sign in if an install was interrupted.";
        else if (!data.http) hint.textContent = "Waiting for the router at 192.168.1.1. Sign in also tries 192.168.100.1 after a finished config.";
        else if (!configReady) hint.textContent = "Pick Config 1 or Config 2.";
        else hint.textContent = "The router is on the LAN. Install stays on 192.168.1.1 until the config uploads, then confirms at 192.168.100.1.";
      }
    };

    var passwordField = document.querySelector('#bench-run input[name="current_password"]');
    if (passwordField) {
      passwordField.addEventListener("input", function () {
        if (last) paint(last);
      });
    }

    var scanStatus = document.getElementById("scan-status");
    var scanHint = document.getElementById("scan-live-hint");
    var scanButton = document.getElementById("scan-label");
    var overlay = document.getElementById("scan-overlay");
    var video = document.getElementById("scan-video");
    var scanStream = null;
    var scanFrame = 0;
    var scanBusy = false;
    var scanNext = 0;
    var liveDetector = null;
    var scanCanvas = document.createElement("canvas");
    var scanContext = scanCanvas.getContext("2d", { willReadFrequently: true });

    var setScanStatus = function (text, kind) {
      if (!scanStatus) return;
      scanStatus.textContent = text || "";
      scanStatus.classList.remove("ok", "bad");
      if (kind) scanStatus.classList.add(kind);
    };

    var setScanHint = function (text) {
      if (scanHint) scanHint.textContent = text || "Point the camera at the QR code on the router.";
    };

    var applyLabel = function (raw) {
      if (!window.TUALabel) return false;
      var parsed;
      try {
        parsed = window.TUALabel.parseLabel(raw);
      } catch (error) {
        var message = window.TUALabel.message(error && error.code);
        setScanStatus(message, "bad");
        setScanHint(message + " Keep the label in view.");
        return false;
      }
      if (!passwordField || !parsed.password) return false;
      passwordField.value = parsed.password;
      passwordField.dispatchEvent(new Event("input", { bubbles: true }));
      var username = document.querySelector('input[name="username"]');
      var note = "Password filled from the label.";
      if (parsed.username && username && username.value !== parsed.username) {
        note += " Username on the label is " + parsed.username + ".";
      }
      setScanStatus(note, "ok");
      return true;
    };

    var readCode = function (source, width, height, sx, sy, sw, sh) {
      if (!window.jsQR || !scanContext) return "";
      var longEdge = Math.max(sw, sh);
      var scale = Math.min(1, 1280 / longEdge);
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
      if (!width || !height) return "";
      var found = readCode(source, width, height, 0, 0, width, height);
      if (found) return found;
      var crop = 0.7;
      var sw = Math.max(1, Math.round(width * crop));
      var sh = Math.max(1, Math.round(height * crop));
      var sx = Math.round((width - sw) / 2);
      var sy = Math.round((height - sh) / 2);
      return readCode(source, width, height, sx, sy, sw, sh);
    };

    var stopLive = function () {
      if (scanFrame) {
        window.cancelAnimationFrame(scanFrame);
        scanFrame = 0;
      }
      scanBusy = false;
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
      setScanHint("Point the camera at the QR code on the router.");
    };

    var onCode = function (raw) {
      if (!raw) return;
      if (applyLabel(raw)) stopLive();
    };

    var tickLive = function (now) {
      scanFrame = window.requestAnimationFrame(tickLive);
      if (scanBusy || !video || video.readyState < 2) return;
      if (now < scanNext) return;
      scanNext = now + 250;
      scanBusy = true;
      var done = function (raw) {
        scanBusy = false;
        onCode(raw);
      };
      var withJsQR = function () {
        done(readBitmap(video, video.videoWidth, video.videoHeight));
      };
      if (liveDetector) {
        liveDetector.detect(video).then(function (codes) {
          var value = codes && codes[0] && codes[0].rawValue;
          if (value) done(value);
          else withJsQR();
        }).catch(withJsQR);
        return;
      }
      withJsQR();
    };

    var openCamera = function (constraints) {
      return navigator.mediaDevices.getUserMedia(constraints);
    };

    if (scanButton) {
      scanButton.addEventListener("click", function () {
        if (!window.isSecureContext) {
          var httpsUrl = "https://" + window.location.host + window.location.pathname + window.location.search;
          setScanStatus("Live scanning needs HTTPS. Open " + httpsUrl + ", accept the certificate, then press Scan the label.", "bad");
          return;
        }
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
          setScanStatus("This browser cannot open the camera for a live scan.", "bad");
          return;
        }
        setScanStatus("Opening the camera…", "");
        setScanHint("Point the camera at the QR code on the router.");
        openCamera({
          audio: false,
          video: {
            facingMode: { ideal: "environment" },
            width: { ideal: 1280 },
            height: { ideal: 720 }
          }
        }).catch(function () {
          return openCamera({ audio: false, video: true });
        }).then(function (stream) {
          scanStream = stream;
          if (video) {
            video.setAttribute("playsinline", "true");
            video.muted = true;
            video.srcObject = stream;
            var play = video.play();
            if (play && play.catch) play.catch(function () { return null; });
          }
          liveDetector = window.BarcodeDetector ? new BarcodeDetector({ formats: ["qr_code"] }) : null;
          if (overlay) {
            overlay.hidden = false;
            overlay.setAttribute("aria-hidden", "false");
          }
          setScanStatus("Scanning for the label…", "");
          scanFrame = window.requestAnimationFrame(tickLive);
        }).catch(function () {
          setScanStatus("The camera did not open. Allow camera access, then try Scan the label again.", "bad");
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
            var message = data.host + " answered on the LAN.";
            var form = document.getElementById("bench-run");
            var install = document.getElementById("install-button");
            var holding = data.phase === "busy" || data.phase === "rebooting" || data.phase === "await-address";
            var startOnLan = form && form.getAttribute("data-start-on-lan") === "1" && install && !install.disabled && !holding;
            if (startOnLan) {
              message += " Install started.";
            }
            if (announce) {
              announce.hidden = false;
              announce.textContent = message;
            }
            beep();
            if (window.Notification && Notification.permission === "granted") {
              new Notification("TUA", { body: message });
            }
            if (startOnLan) install.click();
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
        var submitter = event.submitter;
        var blocked = submitter ? submitter.disabled : false;
        if (!submitter) {
          var install = document.getElementById("install-button");
          blocked = Boolean(install && install.disabled);
        }
        if (blocked) {
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
