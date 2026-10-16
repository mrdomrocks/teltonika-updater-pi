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
