FROM openwa/wa-automate:latest

USER root
RUN mv /etc/apt/sources.list.d/google-chrome.list /tmp/google-chrome.list.disabled 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*
RUN node -e 'const fs=require("fs"); const p="/usr/src/app/node_modules/@open-wa/wa-automate/dist/controllers/popup/index.html"; let s=fs.readFileSync(p,"utf8"); s=s.replace("$(`#${message.sessionId} .qr`)[0].src = message.data", "const qrData = String(message.data || \"\");\\n          document.querySelector(`#${message.sessionId} .qr`).src = qrData.startsWith(\"data:\") ? qrData : \"data:\" + qrData"); fs.writeFileSync(p,s)'
RUN node -e 'const fs=require("fs"); const p="/usr/src/app/node_modules/@open-wa/wa-automate/dist/controllers/initializer.js"; let s=fs.readFileSync(p,"utf8"); s=s.replace("yield waPage.waitForFunction('\''window.Debug!=undefined && window.Debug.VERSION!=undefined && require'\'');", "yield waPage.waitForFunction('\''require'\'');"); fs.writeFileSync(p,s)'
USER owauser
