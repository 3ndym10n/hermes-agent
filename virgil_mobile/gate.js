(() => {
  "use strict";

  const gateId = document.body.dataset.gateId;
  const token = () => new URLSearchParams(location.search).get("t") || "";
  const status = document.getElementById("status");
  const banner = document.getElementById("control-banner");
  const origin = document.getElementById("browser-origin");
  const canvas = document.getElementById("browser-frame");
  const context = canvas.getContext("2d");
  const takeControl = document.getElementById("take-control");
  const tabPicker = document.getElementById("browser-tabs");
  const secureText = document.getElementById("secure-text");
  let socket;
  let csrf = "";
  let controller = false;
  let metadata = { deviceWidth: 1280, deviceHeight: 720 };

  const send = (message) => {
    if (socket?.readyState === WebSocket.OPEN)
      socket.send(JSON.stringify(message));
  };

  const syncTabControl = () => {
    tabPicker.disabled = !controller || tabPicker.options.length < 2;
  };

  const setControl = (active) => {
    controller = active;
    takeControl.hidden = active;
    banner.hidden = active;
    banner.textContent = active
      ? "You have control"
      : "Session controlled elsewhere";
    canvas.classList.toggle("readonly", !active);
    syncTabControl();
  };

  const coordinates = (point) => {
    const rect = canvas.getBoundingClientRect();
    return {
      x: ((point.clientX - rect.left) * metadata.deviceWidth) / rect.width,
      y: ((point.clientY - rect.top) * metadata.deviceHeight) / rect.height,
    };
  };

  const connect = () => {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(
      `${scheme}://${location.host}/api/gate/${encodeURIComponent(gateId)}/stream?t=${encodeURIComponent(token())}`,
    );
    socket.addEventListener("message", (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (message.type === "session") {
        setControl(Boolean(message.controller));
        origin.textContent = message.origin || "Browser session";
        status.textContent = "Secure browser connected.";
      } else if (message.type === "status") {
        metadata = {
          ...metadata,
          deviceWidth: Number(message.viewportWidth) || metadata.deviceWidth,
          deviceHeight: Number(message.viewportHeight) || metadata.deviceHeight,
        };
        status.textContent = message.connected
          ? "Secure browser connected."
          : "Browser session unavailable; the gate remains paused.";
      } else if (message.type === "tabs" && Array.isArray(message.tabs)) {
        tabPicker.replaceChildren();
        for (const tab of message.tabs) {
          if (typeof tab.tabId !== "string" || typeof tab.title !== "string")
            continue;
          const option = document.createElement("option");
          option.value = tab.tabId;
          option.textContent = tab.title;
          option.selected = Boolean(tab.active);
          tabPicker.append(option);
        }
        if (!tabPicker.options.length) {
          const option = document.createElement("option");
          option.textContent = "No browser tabs";
          tabPicker.append(option);
        }
        syncTabControl();
      } else if (message.type === "control") {
        setControl(Boolean(message.controller));
      } else if (message.type === "frame" && typeof message.data === "string") {
        metadata = { ...metadata, ...(message.metadata || {}) };
        const image = new Image();
        image.onload = () => {
          canvas.width = Number(metadata.deviceWidth) || image.naturalWidth;
          canvas.height = Number(metadata.deviceHeight) || image.naturalHeight;
          context.drawImage(image, 0, 0, canvas.width, canvas.height);
        };
        image.src = `data:image/jpeg;base64,${message.data}`;
      } else if (message.type === "input_ack") {
        status.textContent = "Text sent to the focused browser field.";
      } else if (message.type === "tab_ack") {
        if (typeof message.origin === "string")
          origin.textContent = message.origin;
        status.textContent = "Browser tab selected.";
      } else if (message.type === "gate_closed") {
        status.textContent =
          "This gate has closed. Return to Telegram for current status.";
      } else if (message.type === "error") {
        status.textContent =
          "The browser session is unavailable. Your gate remains safely paused.";
      }
    });
    socket.addEventListener("close", () => {
      if (!status.textContent.includes("closed"))
        status.textContent = "Browser disconnected. Reload to reconnect.";
    });
  };

  const post = async (action) => {
    const response = await fetch(
      `/api/gate/${encodeURIComponent(gateId)}/${action}?t=${encodeURIComponent(token())}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: "{}",
        cache: "no-store",
      },
    );
    if (!response.ok) throw new Error("gate_request_failed");
    return response.json();
  };

  canvas.addEventListener("pointerdown", (event) => {
    if (!controller || event.pointerType === "touch") return;
    canvas.setPointerCapture(event.pointerId);
    send({
      type: "input_mouse",
      eventType: "mousePressed",
      ...coordinates(event),
      button: "left",
      clickCount: 1,
    });
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!controller || event.pointerType === "touch" || event.buttons !== 1)
      return;
    send({
      type: "input_mouse",
      eventType: "mouseMoved",
      ...coordinates(event),
    });
  });
  canvas.addEventListener("pointerup", (event) => {
    if (!controller || event.pointerType === "touch") return;
    send({
      type: "input_mouse",
      eventType: "mouseReleased",
      ...coordinates(event),
      button: "left",
      clickCount: 1,
    });
  });
  canvas.addEventListener(
    "wheel",
    (event) => {
      if (!controller) return;
      event.preventDefault();
      send({
        type: "input_mouse",
        eventType: "mouseWheel",
        ...coordinates(event),
        deltaX: event.deltaX,
        deltaY: event.deltaY,
      });
    },
    { passive: false },
  );

  const touches = (event) => Array.from(event.touches).map(coordinates);
  for (const [domType, eventType] of [
    ["touchstart", "touchStart"],
    ["touchmove", "touchMove"],
    ["touchend", "touchEnd"],
    ["touchcancel", "touchCancel"],
  ]) {
    canvas.addEventListener(
      domType,
      (event) => {
        if (!controller) return;
        event.preventDefault();
        send({ type: "input_touch", eventType, touchPoints: touches(event) });
      },
      { passive: false },
    );
  }

  canvas.addEventListener("keydown", (event) => {
    if (!controller || event.key.length === 1) return;
    event.preventDefault();
    send({
      type: "input_keyboard",
      eventType: "keyDown",
      key: event.key,
      code: event.code,
    });
  });
  canvas.addEventListener("keyup", (event) => {
    if (!controller || event.key.length === 1) return;
    event.preventDefault();
    send({
      type: "input_keyboard",
      eventType: "keyUp",
      key: event.key,
      code: event.code,
    });
  });

  document.getElementById("text-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!controller || !secureText.value) return;
    send({ type: "input_text", text: secureText.value });
    secureText.value = "";
  });
  takeControl.addEventListener("click", () => send({ type: "take_control" }));
  tabPicker.addEventListener("change", () => {
    if (!controller || !/^t[1-9][0-9]{0,5}$/.test(tabPicker.value)) return;
    send({ type: "select_tab", tab_id: tabPicker.value });
  });
  document.getElementById("done").addEventListener("click", async () => {
    try {
      await post("done");
      status.textContent =
        "Verification requested. Virgil will resume only after the provider check passes.";
    } catch {
      status.textContent =
        "Could not request verification. The gate remains safely paused.";
    }
  });
  document.getElementById("renew").addEventListener("click", async () => {
    try {
      const result = await post("renew");
      const url = new URL(location.href);
      url.searchParams.set("t", result.token);
      history.replaceState(null, "", url);
      socket?.close();
      connect();
      status.textContent = "Private link renewed for up to 30 minutes.";
    } catch {
      status.textContent =
        "Could not renew this link. Return to Telegram for a fresh link.";
    }
  });

  fetch("/api/session", { cache: "no-store" })
    .then((response) => (response.ok ? response.json() : Promise.reject()))
    .then((session) => {
      csrf = session.csrf_token;
      connect();
    })
    .catch(() => {
      status.textContent = "Private session authentication failed.";
    });
})();
