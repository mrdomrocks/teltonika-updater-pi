/* Teltonika label QR codes. Keep this in step with rutkeeper/label.py. */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.TUALabel = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  var messages = {
    empty: "That code was empty.",
    "no-password": "That label has no device password. Type the password printed on the router.",
    unrecognized: "That code is not a router label. Photograph the QR code on the device.",
    "no-code": "No QR code was found in that photo. Hold the phone steady and fill the frame with the label.",
    "unreadable": "That photo could not be read. Take another picture of the QR code."
  };

  function splitUnescaped(text, sep) {
    var parts = [];
    var buf = "";
    var escaped = false;
    for (var i = 0; i < text.length; i += 1) {
      var ch = text.charAt(i);
      if (escaped) {
        buf += "\\" + ch;
        escaped = false;
        continue;
      }
      if (ch === "\\") {
        escaped = true;
        continue;
      }
      if (ch === sep) {
        parts.push(buf);
        buf = "";
        continue;
      }
      buf += ch;
    }
    if (escaped) buf += "\\";
    parts.push(buf);
    return parts;
  }

  function unescapeValue(value) {
    var out = "";
    var escaped = false;
    for (var i = 0; i < value.length; i += 1) {
      var ch = value.charAt(i);
      if (escaped) {
        out += ch;
        escaped = false;
      } else if (ch === "\\") {
        escaped = true;
      } else {
        out += ch;
      }
    }
    if (escaped) out += "\\";
    return out;
  }

  function looksLikeFields(body) {
    var stripped = body.replace(/^\s+/, "");
    var mark = stripped.indexOf(":");
    if (mark <= 0) return false;
    var key = stripped.slice(0, mark);
    return /^[A-Za-z]{1,8}$/.test(key);
  }

  function parseLabel(text) {
    var raw = String(text || "").replace(/^\uFEFF/, "").replace(/\u0000/g, "").trim();
    if (!raw) {
      var empty = new Error("empty");
      empty.code = "empty";
      throw empty;
    }
    if (/^https?:\/\//i.test(raw)) fail("unrecognized");

    var body = raw.slice(0, 5).toUpperCase() === "WIFI:" ? raw.slice(5) : raw;
    if (body.indexOf(";") !== -1 || looksLikeFields(body)) {
      var fields = {};
      splitUnescaped(body, ";").forEach(function (part) {
        part = part.trim();
        if (!part) return;
        var bits = splitUnescaped(part, ":");
        if (bits.length < 2) return;
        var key = bits[0].trim().toUpperCase();
        var value = unescapeValue(bits.slice(1).join(":")).trim();
        if (key) fields[key] = value;
      });
      if (fields.PW) return { password: fields.PW, username: fields.U || "" };
      if (Object.keys(fields).length) fail("no-password");
    }

    if (raw.indexOf(";") === -1 && raw.length <= 128) return { password: raw, username: "" };
    fail("unrecognized");
  }

  function fail(code) {
    var error = new Error(code);
    error.code = code;
    throw error;
  }

  return {
    parseLabel: parseLabel,
    message: function (code) {
      return messages[code] || messages.unrecognized;
    }
  };
});
