const { app, BrowserWindow, shell } = require("electron");
const { spawn, execSync } = require("child_process");
const path = require("path");
const http = require("http");

let mainWindow;
let pythonProcess;
const PORT = 8765;

function startPythonServer() {
  try { execSync(`lsof -ti:${PORT} | xargs kill -9 2>/dev/null`); } catch (_) {}

  const scriptPath = path.join(__dirname, "server.py");
  pythonProcess = spawn("/opt/homebrew/bin/python3", [scriptPath], {
    cwd: __dirname,
    stdio: "pipe",
  });
  pythonProcess.stdout.on("data", (d) => console.log(`[server] ${d}`));
  pythonProcess.stderr.on("data", (d) => console.error(`[server err] ${d}`));
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 680,
    height: 640,
    titleBarStyle: "default",
    backgroundColor: "#0d1117",
    webPreferences: { nodeIntegration: false, contextIsolation: true },
    title: "YT Downloader",
    show: false,
  });

  // Show a loading screen immediately
  mainWindow.loadURL(`data:text/html,
    <style>
      body { margin:0; background:#0d1117; display:flex; align-items:center;
             justify-content:center; height:100vh; font-family:-apple-system,sans-serif; }
      p { color:#484f58; font-size:14px; }
      .dot { animation: blink 1s infinite; }
      @keyframes blink { 0%,100%{opacity:0.2} 50%{opacity:1} }
    </style>
    <p>Starting<span class="dot">...</span></p>
  `);

  mainWindow.once("ready-to-show", () => mainWindow.show());

  // Don't destroy the window on close — just hide it (standard macOS behaviour)
  mainWindow.on("close", (e) => {
    e.preventDefault();
    mainWindow.hide();
  });

  // Poll until server is ready, then load the real app
  let attempts = 0;
  const poll = setInterval(() => {
    attempts++;
    const req = http.get(`http://localhost:${PORT}/`, (res) => {
      if (res.statusCode === 200) {
        clearInterval(poll);
        mainWindow.loadURL(`http://localhost:${PORT}/`);
      }
    });
    req.on("error", () => {});
    req.end();
    if (attempts > 40) clearInterval(poll); // give up after 12s
  }, 300);
}

app.whenReady().then(() => {
  startPythonServer();
  createWindow();
});

app.on("window-all-closed", () => {
  if (pythonProcess) pythonProcess.kill();
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (mainWindow) {
    mainWindow.show();
  } else {
    createWindow();
  }
});

app.on("before-quit", () => {
  if (pythonProcess) pythonProcess.kill();
});
