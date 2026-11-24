const byId = (id) => document.getElementById(id);
const dateInput = byId("date");
const transcript = byId("transcript");
const timeFormat = new Intl.DateTimeFormat("en-IN", { hour: "numeric", minute: "2-digit", timeZone: "UTC" });
const dateFormat = new Intl.DateTimeFormat("en-IN", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
let dashboard;
let highlight;
let lastRequest = 0;
let call;
const filePreview = location.protocol === "file:";

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

const displayDate = (date) => dateFormat.format(new Date(date));
const displayTime = (time) => timeFormat.format(new Date("2000-01-01T" + time + "Z"));

function renderDashboard() {
  if (!dashboard) return;
  const cards = dashboard.doctors.map((doctor) => {
    const row = element("article", "", "doctor");
    const identity = element("div", "", "identity");
    const name = element("div");
    name.append(element("strong", doctor.name), element("p", doctor.specialty, "specialty"));
    identity.append(element("span", doctor.initials, "initials"), name);
    const slots = element("div", "", "slots");
    const available = doctor.slots.filter((slot) => slot.date === dateInput.value);
    slots.append(...(available.length ? available.map((slot) => element("span", displayTime(slot.time), "slot"))
      : [element("p", "No open slots", "empty")]));
    row.append(identity, slots);
    return row;
  });
  byId("doctors").replaceChildren(...cards);
  const list = dashboard.appointments;
  byId("count").textContent = String(list.length) + " active";
  if (!list.length) {
    byId("appointments").replaceChildren(element("p", "No appointments booked yet.", "empty"));
    return;
  }
  const wrapper = element("div", "", "table-wrap");
  const table = element("table");
  const header = table.createTHead().insertRow();
  for (const label of ["Doctor", "When (IST)", "Caller"]) {
    const cell = element("th", label);
    cell.scope = "col";
    header.append(cell);
  }
  const body = table.createTBody();
  for (const appointment of list) {
    const row = body.insertRow();
    if (appointment.id === highlight) row.className = "updated";
    row.insertCell().textContent = appointment.doctor;
    row.insertCell().textContent = displayDate(appointment.date) + " · " + displayTime(appointment.time);
    row.insertCell().textContent = appointment.phone;
  }
  wrapper.append(table);
  byId("appointments").replaceChildren(wrapper);
  highlight = undefined;
}

async function refresh() {
  const request = ++lastRequest;
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error("Schedule request failed");
    const data = await response.json();
    if (request !== lastRequest) return;
    dashboard = data;
    byId("today").textContent = displayDate(data.today);
    dateInput.min = data.today;
    const last = new Date(data.today);
    last.setUTCDate(last.getUTCDate() + 6);
    dateInput.max = last.toISOString().slice(0, 10);
    if (!dateInput.value || dateInput.value < data.today) dateInput.value = data.today;
    renderDashboard();
    byId("schedule-status").textContent = "";
  } catch {
    if (request === lastRequest) byId("schedule-status").textContent = "Schedule unavailable. Retrying shortly.";
  }
}

function showPreview() {
  const today = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts();
  const parts = Object.fromEntries(today.map(({ type, value }) => [type, value]));
  const date = [parts.year, parts.month, parts.day].join("-");
  const doctors = [
    ["Dr. Meera Rao", "General medicine", "MR", ["09:30", "11:00", "14:30"]],
    ["Dr. Arjun Shah", "Cardiology", "AS", ["09:30", "14:30", "16:00"]],
    ["Dr. Nisha Patel", "Dermatology", "NP", ["11:00", "14:30", "16:00"]],
  ];
  dashboard = {
    today: date,
    doctors: doctors.map(([name, specialty, initials, times]) => ({
      name, specialty, initials, slots: times.map((time) => ({ date, time })),
    })),
    appointments: [],
  };
  byId("today").textContent = displayDate(date);
  byId("environment").textContent = "Sample preview · IST";
  dateInput.value = date;
  dateInput.disabled = true;
  byId("call-status").textContent = "Local preview";
  transcript.querySelector(".call-empty").textContent = "Start the clinic server and open localhost:8000 to make a voice call.";
  byId("start").disabled = true;
  renderDashboard();
}

function bubble(label, text, kind) {
  transcript.querySelector(".call-empty")?.remove();
  const message = element("div", "", "message " + kind);
  const content = element("span", text);
  message.append(element("strong", label), content);
  transcript.append(message);
  transcript.scrollTop = transcript.scrollHeight;
  return content;
}

function clearAudio(session) {
  for (const source of session.sources) source.stop();
  session.sources.clear();
  session.nextPlay = 0;
}

function play(session, data) {
  const pcm = new Int16Array(data);
  if (!pcm.length) return;
  const buffer = session.context.createBuffer(1, pcm.length, 24000);
  buffer.copyToChannel(Float32Array.from(pcm, (sample) => sample / 32768), 0);
  const source = session.context.createBufferSource();
  source.buffer = buffer;
  source.connect(session.context.destination);
  session.nextPlay = Math.max(session.nextPlay, session.context.currentTime + .045);
  source.start(session.nextPlay);
  session.nextPlay += buffer.duration;
  session.sources.add(source);
  source.onended = () => session.sources.delete(source);
}

function showError(text) {
  byId("call-error").textContent = text;
  byId("call-error").hidden = false;
}

function end(session, status) {
  if (call !== session) return;
  call = undefined;
  clearInterval(session.timer);
  clearAudio(session);
  session.stream?.getTracks().forEach((track) => track.stop());
  if (session.socket && session.socket.readyState < WebSocket.CLOSING) session.socket.close();
  session.context?.close().catch(() => {});
  byId("call-status").textContent = status;
  byId("start").hidden = false;
  byId("start").textContent = "Start another call";
  byId("end").hidden = true;
  byId("mute").hidden = true;
  refresh();
}

function handleEvent(session, { type, text, appointment, action }) {
  switch (type) {
    case "ready":
      if (session.ready) return;
      session.ready = true;
      byId("call-status").textContent = "Connected";
      byId("mute").hidden = false;
      session.started = performance.now();
      session.timer = setInterval(() => {
        const elapsed = Math.floor((performance.now() - session.started) / 1000);
        const pad = (value) => String(value).padStart(2, "0");
        byId("timer").textContent = pad(Math.floor(elapsed / 60)) + ":" + pad(elapsed % 60);
      }, 250);
      break;
    case "caller":
      bubble("You", text, "caller");
      break;
    case "assistant_start":
    case "assistant_end":
      session.reply = undefined;
      break;
    case "assistant_text":
      if (!session.reply) session.reply = bubble("Cedar", "", "assistant");
      session.reply.textContent += text;
      transcript.scrollTop = transcript.scrollHeight;
      break;
    case "clear":
      clearAudio(session);
      session.reply = undefined;
      break;
    case "appointment": {
      const booked = action === "book_appointment";
      if (booked) highlight = appointment.id;
      const label = booked ? "Appointment booked" : "Appointment cancelled";
      const when = displayDate(appointment.date) + " · " + displayTime(appointment.time);
      bubble(label, appointment.doctor + " · " + when, "event");
      refresh();
      break;
    }
    case "error":
      showError(text);
      break;
  }
}

async function start() {
  if (call) return;
  const session = { sources: new Set(), nextPlay: 0, muted: false };
  call = session;
  transcript.replaceChildren(element("p", "Your conversation will appear here.", "call-empty"));
  byId("call-error").hidden = true;
  byId("start").hidden = true;
  byId("end").hidden = false;
  byId("mute").textContent = "Mute";
  byId("mute").setAttribute("aria-pressed", "false");
  byId("call-status").textContent = "Connecting…";
  byId("timer").textContent = "00:00";
  let errorText = "Could not open the microphone. Check browser permissions and use localhost or HTTPS.";
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("Microphone unavailable");
    session.context = new AudioContext();
    await session.context.resume();
    if (call !== session) return;
    session.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    if (call !== session) {
      session.stream.getTracks().forEach((track) => track.stop());
      return;
    }
    errorText = "Could not initialize the browser audio stream.";
    await session.context.audioWorklet.addModule("./capture.js");
    if (call !== session) return;
    const worklet = new AudioWorkletNode(session.context, "capture", { channelCount: 1, channelCountMode: "explicit" });
    const silent = session.context.createGain();
    silent.gain.value = 0;
    session.context.createMediaStreamSource(session.stream).connect(worklet).connect(silent).connect(session.context.destination);

    errorText = "Could not connect to the voice server.";
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    session.socket = new WebSocket(protocol + "//" + location.host + "/ws/call");
    session.socket.binaryType = "arraybuffer";
    session.socket.onmessage = ({ data }) => {
      if (call !== session) return;
      if (typeof data === "string") handleEvent(session, JSON.parse(data));
      else play(session, data);
    };
    session.socket.onerror = () => {
      if (call === session) showError("The connection to the voice server failed.");
    };
    session.socket.onclose = ({ code }) => {
      if (call !== session) return;
      if (code !== 1000 && byId("call-error").hidden) showError("The call disconnected unexpectedly.");
      end(session, session.ready ? "Call ended" : "Call unavailable");
    };
    worklet.port.onmessage = ({ data }) => {
      if (call === session && session.ready && session.socket.readyState === WebSocket.OPEN
          && session.socket.bufferedAmount < 16000) {
        session.socket.send(session.muted ? new ArrayBuffer(data.byteLength) : data);
      }
    };
  } catch {
    if (call === session) {
      showError(errorText);
      end(session, "Call unavailable");
    }
  }
}

byId("start").addEventListener("click", start);
byId("end").addEventListener("click", () => end(call, "Call ended"));
byId("mute").addEventListener("click", () => {
  call.muted = !call.muted;
  byId("mute").textContent = call.muted ? "Unmute" : "Mute";
  byId("mute").setAttribute("aria-pressed", String(call.muted));
});
dateInput.addEventListener("change", renderDashboard);
if (filePreview) {
  showPreview();
} else {
  refresh();
  setInterval(refresh, 15000);
}
