(function () {
  "use strict";

  const ui = {};
  const transient = {
    pending: false,
    listening: false,
    fetchError: false,
    awaySince: 0,
    justReturnedUntil: 0,
    previousPresence: null,
    debugState: null
  };
  let currentStatus = null;
  let chatSignature = "";
  let refreshTimer = null;
  let sensorTimer = null;
  let settingsTimer = null;
  let recognition = null;
  let recording = false;
  let lastPlayfulMotion = -1;
  let playfulMotionTimer = null;
  let idleDriftTimer = null;
  let currentRoleName = "陪伴助手";
  let currentStreamVoiceLabel = "";
  let streamVoicesLoading = false;
  let currentPersonaId = "default";
  let personaPresetsById = {};
  let personaFormDirty = false;
  const playfulMotions = ["motion-hop", "motion-wiggle", "motion-pop", "motion-step", "motion-cheer"];

  function byId(id) { return document.getElementById(id); }
  function safeText(value, fallback) {
    const text = String(value == null ? "" : value).trim();
    return text || fallback || "";
  }
  function escapeHtml(text) {
    return String(text || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }
  function includesAny(value, words) {
    const text = String(value || "").toLowerCase();
    return words.some((word) => text.includes(word.toLowerCase()));
  }
  function setText(element, value) { if (element) element.textContent = value; }
  function applyRoleName(value) {
    currentRoleName = safeText(value, "陪伴助手");
    if (ui.roleNameInput && !personaFormDirty && document.activeElement !== ui.roleNameInput) {
      ui.roleNameInput.value = currentRoleName;
    }
    setText(ui.sleepCurtainTitle, `${currentRoleName}正在休息。`);
    setText(ui.sleepCurtainText, "全程静默中，可通过语音唤醒。");
    setText(ui.proactiveDescription, `让${currentRoleName}在合适的时候主动和你说话`);
    if (ui.chatInput) ui.chatInput.setAttribute("aria-label", `对${currentRoleName}说话`);
  }

  function cacheElements() {
    [
      "userTag", "nuanyuScene", "nuanyuCharacter", "nuanyuSvgTitle", "visualStateLabel", "characterStatus",
      "strategyText", "stageFocus", "stageFocusTime", "stageFocusGoal", "moodDock", "focusRibbonLabel",
      "focusGoal", "focusTime", "returnBanner", "chatBox", "chatInput", "micBtn", "sendButton",
      "recordingStatus", "focusAction", "chatAction", "todayAction", "moodAction", "todaySummary",
      "systemDetails", "systemSummary", "healthMessage", "cameraOk", "voiceOk", "aiOk", "visualState",
      "pipeTTS", "pipeSpeaker", "emotionDisplay", "lastVoice", "scheduleBox", "scheduleList",
      "eventList", "debugControls", "studyModalMask", "goalInput", "cancelStudyButton", "startStudyButton",
      "presencePill", "liveStatusRail", "sleepButton", "sleepCurtain",
      "sleepCurtainTitle", "sleepCurtainText", "proactiveDescription",
      "sensorPanel", "sensorHealth", "sensorPresenceItem", "sensorPresence",
      "sensorTemperatureItem", "sensorTemperature", "sensorHumidityItem", "sensorHumidity",
      "sensorAirItem", "sensorAir", "sensorLightItem", "sensorLight",
      "currentPersona", "currentVoice", "memoryCount", "messageCount",
      "roleNameInput", "saveRoleNameButton", "roleNameHint",
      "personalityInput", "languageStyleInput", "catchphrasesInput", "characterPresetSelector",
      "proactiveStatusLabel", "proactiveToggle", "ttsModeSelect",
      "micMuteToggle", "micState", "proactiveLevel", "proactiveLevelValue",
      "streamVoiceGroup", "streamVoiceSelector", "streamPreviewBtn", "streamPreviewHint",
      "surgeVoiceSettingsGroup", "manageSurgeVoiceButton"
    ].forEach((id) => { ui[id] = byId(id); });
  }

  function renderChat(history) {
    if (!Array.isArray(history)) return;
    const signature = JSON.stringify(history.map((item) => [
      item.time || "",
      item.role || "",
      item.text || "",
      item.ai_ms ?? null,
      item.tts_ms ?? null,
      item.tts_pending ?? null,
      item.tts_ok ?? null,
    ]));
    if (signature === chatSignature) return;
    chatSignature = signature;
    ui.chatBox.innerHTML = "";
    if (!history.length) {
      ui.chatBox.innerHTML = '<div class="empty-chat"><div>这里暂时是空的。<br>从此刻想到的事开始。</div></div>';
      return;
    }
    history.forEach((item, idx) => {
      const message = document.createElement("div");
      const role = item.role === "user" ? "user" : "assistant";
      const cardType = ["report", "proactive"].includes(item.card_type) ? ` ${item.card_type}` : "";
      message.className = `message ${role}${cardType}`;
      let bubbleHtml = `<div class="bubble">${escapeHtml(item.text)}</div>`;
      if (role === "assistant" && (item.tts_pending ||
          item.tts_ms != null || item.tts_ok === false)) {
        const latencyParts = [];
        if (item.tts_pending) {
          latencyParts.push("首音 计算中…");
        } else if (item.tts_ms != null) {
          latencyParts.push(`首音 ${Math.round(Number(item.tts_ms))}ms`);
        } else if (item.tts_ok === false) {
          latencyParts.push("首音失败");
        }
        bubbleHtml += `<div class="tts-latency">${latencyParts.join(" · ")}</div>`;
      }
      message.innerHTML = bubbleHtml;
      ui.chatBox.appendChild(message);
    });
    requestAnimationFrame(() => { ui.chatBox.scrollTop = ui.chatBox.scrollHeight; });
  }

  function renderSchedule(list) {
    const hasSchedule = Array.isArray(list) && list.length > 0;
    ui.scheduleBox.hidden = !hasSchedule;
    if (!hasSchedule) return;
    ui.scheduleList.innerHTML = "";
    list.forEach((reminder) => {
      const date = new Date(Number(reminder.trigger_time || 0) * 1000);
      const time = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
      const item = document.createElement("div");
      item.className = "event";
      item.innerHTML = `<b>${escapeHtml(time)}</b>　${escapeHtml(reminder.content)}`;
      ui.scheduleList.appendChild(item);
    });
  }

  function renderEvents(events) {
    ui.eventList.innerHTML = "";
    if (!Array.isArray(events) || !events.length) {
      ui.eventList.innerHTML = '<div class="event">今天还没有新的事件。</div>';
      return;
    }
    events.slice().reverse().slice(0, 10).forEach((event) => {
      const item = document.createElement("div");
      item.className = "event";
      item.innerHTML = `<b>${escapeHtml(event.time)} · ${escapeHtml(event.type)}</b><br>${escapeHtml(event.text)}`;
      ui.eventList.appendChild(item);
    });
  }

  function updatePresence(status) {
    const present = status.visual_state === "PRESENT";
    if (transient.previousPresence === false && present) transient.justReturnedUntil = Date.now() + 5000;
    transient.previousPresence = present;
    if (!present) {
      if (!transient.awaySince) transient.awaySince = Date.now();
    } else {
      transient.awaySince = 0;
    }
    transient.justReturned = Date.now() < transient.justReturnedUntil;
    transient.awaySeconds = transient.awaySince ? (Date.now() - transient.awaySince) / 1000 : 0;
  }

  function applyVisualState(status) {
    updatePresence(status);
    const resolved = window.NuanyuVisualState.resolve(status, transient);
    const visualState = transient.debugState || resolved.state;
    const copy = transient.debugState
      ? window.NuanyuVisualState.STATE_COPY[transient.debugState]
      : resolved.copy;
    const label = window.NuanyuVisualState.STATE_LABEL[visualState];
    ui.nuanyuCharacter.dataset.state = visualState;
    setText(ui.nuanyuSvgTitle, `${currentRoleName}${label}`);
    setText(ui.visualStateLabel, label);
    setText(ui.characterStatus, copy);
    setText(ui.strategyText, naturalStrategy(status, visualState));
    document.body.dataset.visualState = visualState;
  }

  function naturalStrategy(status, state) {
    if (state === "sleep") return "正在等待语音唤醒。";
    if (state === "error") return "文字对话仍然可用。";
    if (state === "focus") return safeText(status.study_goal, "专注学习");
    if (status.current_mood === "开心") return "把这份轻松留一会儿。";
    if (status.current_mood === "疲惫") return "今天可以少做一点。";
    if (["焦虑", "崩溃", "低落"].includes(status.current_mood)) return "先只看眼前这一步。";
    return "我在。";
  }

  function clearPlayfulMotion() {
    playfulMotions.forEach((name) => ui.nuanyuCharacter.classList.remove(name));
    ui.nuanyuCharacter.classList.remove("is-playful");
    ui.nuanyuScene.classList.remove("is-playful");
    if (playfulMotionTimer) clearTimeout(playfulMotionTimer);
    playfulMotionTimer = null;
  }

  function randomBetween(min, max) {
    return min + Math.random() * (max - min);
  }

  function scheduleIdleDrift(delay) {
    if (idleDriftTimer) clearTimeout(idleDriftTimer);
    idleDriftTimer = setTimeout(() => {
      const character = ui.nuanyuCharacter;
      if (!document.hidden && character.dataset.state === "idle" && !character.classList.contains("is-playful")) {
        const duration = Math.round(randomBetween(2800, 5100));
        character.style.setProperty("--idle-drift-duration", `${duration}ms`);
        character.style.setProperty("--idle-drift-x", `${randomBetween(-7, 7).toFixed(1)}px`);
        character.style.setProperty("--idle-drift-y", `${randomBetween(-10, 3).toFixed(1)}px`);
        character.style.setProperty("--idle-drift-rotate", `${randomBetween(-0.75, 0.75).toFixed(2)}deg`);
        character.style.setProperty("--idle-drift-scale", randomBetween(0.996, 1.012).toFixed(3));
      }
      scheduleIdleDrift(Math.round(randomBetween(3000, 5400)));
    }, delay);
  }

  function playRandomMotion() {
    if (!currentStatus || currentStatus.assistant_sleeping) return;
    clearPlayfulMotion();
    let next = Math.floor(Math.random() * playfulMotions.length);
    if (playfulMotions.length > 1 && next === lastPlayfulMotion) next = (next + 1 + Math.floor(Math.random() * (playfulMotions.length - 1))) % playfulMotions.length;
    lastPlayfulMotion = next;
    // Force a fresh animation even when the user clicks rapidly.
    void ui.nuanyuCharacter.getBoundingClientRect();
    ui.nuanyuCharacter.classList.add("is-playful", playfulMotions[next]);
    ui.nuanyuScene.classList.add("is-playful");
    playfulMotionTimer = setTimeout(clearPlayfulMotion, 1100);
  }

  function setMoodButtons(mood) {
    document.querySelectorAll(".mood-button").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.mood === mood));
    });
  }

  function renderFocus(status) {
    const active = Boolean(status.study_running);
    const goal = safeText(status.study_goal, "专注学习");
    setText(ui.focusRibbonLabel, active ? "正在一起专注" : "今天还没有开始专注");
    setText(ui.focusGoal, active ? goal : "要不要先试一轮？");
    setText(ui.focusTime, active ? safeText(status.study_time, "刚刚开始") : "未开启");
    setText(ui.focusAction, active ? "结束专注" : "开始专注");
    ui.stageFocus.classList.toggle("visible", active);
    setText(ui.stageFocusTime, safeText(status.study_time, "刚刚开始"));
    setText(ui.stageFocusGoal, goal);
  }

  function renderToday(status) {
    const daily = status.daily || {};
    const study = safeText(daily.study_time, "0秒");
    const webCount = Number(daily.web_interactions || 0);
    const voiceCount = Number(daily.voice_interactions || 0);
    let text = "<strong>今天刚刚开始。</strong>";
    if (!includesAny(study, ["0秒", "未开启"])) {
      text = `<strong>今日专注 ${escapeHtml(study)}。</strong>`;
    } else if (webCount + voiceCount > 0) {
      text = `<strong>今天有 ${webCount + voiceCount} 次对话。</strong>`;
    }
    ui.todaySummary.innerHTML = text;
  }

  function moduleHealth(status) {
    const issues = window.NuanyuVisualState.collectIssues(status);
    const browserSpeechReady = Boolean(window.SpeechRecognition || window.webkitSpeechRecognition);
    const away = status.visual_state === "AWAY";
    let summary = "所有功能已准备好 · 查看详情";
    let message = "所有功能已准备好。";
    if (issues.length) {
      summary = "有一个功能需要留意 · 查看详情";
      message = issues.join(" ");
    } else if (away && !status.camera_ok) {
      summary = `${currentRoleName}正在安静等你 · 查看详情`;
      message = "我现在没有看到你，回来后叫我一声。";
    } else if (!status.voice_ok && !browserSpeechReady) {
      summary = "语音暂时不可用 · 查看详情";
      message = "我的语音暂时没连上，不过你还可以打字和我说话。";
    }
    setText(ui.systemSummary, summary);
    setText(ui.healthMessage, message);
    ui.healthMessage.classList.toggle("warning", issues.length > 0 || (!status.voice_ok && !browserSpeechReady));
  }

  function friendlyPipeline(value, kind) {
    const text = safeText(value, "待机");
    if (kind === "tts" && includesAny(text, ["板载TTS", "已播放", "已预加载"])) return "准备好了";
    if (kind === "speaker" && includesAny(text, ["已就绪", "扬声器"])) return "准备好了";
    if (includesAny(text, ["生成中", "合成中", "播放中", "排队中"])) return "正在工作";
    if (includesAny(text, ["失败", "错误", "离线"])) return "需要检查";
    return text;
  }

  function renderDevices(status) {
    const vision = status.vision || {};
    const browserSpeechReady = Boolean(window.SpeechRecognition || window.webkitSpeechRecognition);
    setText(ui.cameraOk, status.camera_ok ? "正在感知" : "暂未看到你");
    setText(ui.voiceOk, status.assistant_sleeping ? "仅待唤醒" : (status.voice_ok || browserSpeechReady ? "可以听见" : "暂不可用"));
    setText(ui.aiOk, status.assistant_sleeping ? "休息中" : (status.ai_ok ? "可以回应" : "等待第一次对话"));
    setText(ui.visualState, status.visual_state === "PRESENT" ? "你在这里" : "等你回来");
    setText(ui.pipeTTS, friendlyPipeline((status.pipeline || {}).tts, "tts"));
    setText(ui.pipeSpeaker, friendlyPipeline((status.pipeline || {}).speaker, "speaker"));
    setText(ui.emotionDisplay, safeText(vision.emotion_zh, "保持平静"));
    setText(ui.lastVoice, status.last_voice_cmd && status.last_voice_cmd !== "none" ? status.last_voice_cmd : "还没有说话");
    ui.cameraOk.classList.toggle("is-warning", !status.camera_ok);
    ui.voiceOk.classList.toggle("is-warning", !(status.voice_ok || browserSpeechReady));
    ui.aiOk.classList.toggle("is-warning", !status.ai_ok);
    ui.liveStatusRail.classList.toggle("has-issue", !status.camera_ok || !(status.voice_ok || browserSpeechReady));
    moduleHealth(status);
  }

  function setSensorValue(item, output, available, text) {
    if (!item || !output) return;
    item.hidden = !available;
    if (available) setText(output, text);
  }

  function renderSensors(payload) {
    const sensors = (payload && payload.sensors) || {};
    const health = (payload && payload.health) || {};
    const available = health.available || {};
    const age = Number(health.last_update_sec);
    const fresh = health.last_update_sec != null && Number.isFinite(age) && age < 7;
    const connected = Boolean(health.connected) && fresh;
    const radarOnline = connected && Boolean(health.radar_online);

    ui.sensorHealth.classList.toggle("offline", !connected);
    ui.sensorHealth.classList.toggle("waiting", Boolean(health.connected) && !fresh);
    if (!health.connected) setText(ui.sensorHealth, "传感器离线");
    else if (!fresh) setText(ui.sensorHealth, "等待新数据");
    else if (!radarOnline) setText(ui.sensorHealth, "环境在线 · 雷达离线");
    else setText(ui.sensorHealth, "传感器在线");

    setSensorValue(
      ui.sensorPresenceItem,
      ui.sensorPresence,
      radarOnline && Boolean(available.presence),
      sensors.presence ? "检测到人" : "当前无人"
    );
    setSensorValue(
      ui.sensorTemperatureItem,
      ui.sensorTemperature,
      connected && Boolean(available.temperature_c),
      `${Number(sensors.temperature_c).toFixed(1)} °C`
    );
    setSensorValue(
      ui.sensorHumidityItem,
      ui.sensorHumidity,
      connected && Boolean(available.humidity_rh),
      `${Number(sensors.humidity_rh).toFixed(1)} %RH`
    );
    setSensorValue(
      ui.sensorAirItem,
      ui.sensorAir,
      connected && Boolean(available.air_ppb),
      `${Math.round(Number(sensors.air_ppb))} ppb`
    );
    setSensorValue(
      ui.sensorLightItem,
      ui.sensorLight,
      connected && Boolean(available.light),
      `${Math.round(Number(sensors.light))} lx`
    );
  }

  async function refreshSensors() {
    try {
      const payload = await fetchJson("/api/sensors", { cache: "no-store" });
      renderSensors(payload);
    } catch (error) {
      renderSensors({ health: { connected: false, available: {} }, sensors: {} });
      console.warn("[Nuanyu] sensor refresh temporarily failed", error);
    } finally {
      clearTimeout(sensorTimer);
      sensorTimer = setTimeout(refreshSensors, document.hidden ? 6000 : 2000);
    }
  }

  function renderStatus(status) {
    currentStatus = status;
    transient.fetchError = false;
    applyRoleName(status.assistant_name);
    const nickname = safeText((status.memory || {}).nickname, safeText(status.username, "同学"));
    setText(ui.userTag, nickname);
    setMoodButtons(status.current_mood || "一般");
    renderFocus(status);
    renderToday(status);
    renderDevices(status);
    renderMicControl(status);
    renderQuietMode(status);
    renderChat(status.chat_history || []);
    renderSchedule(status.schedule || []);
    renderEvents(status.events || []);
    const sleeping = Boolean(status.assistant_sleeping);
    ui.returnBanner.classList.toggle("visible", Boolean(status.return_prompt) && !sleeping);
    ui.sleepCurtain.setAttribute("aria-hidden", String(!sleeping));
    const quiet = Boolean(status.quiet_mode || sleeping);
    ui.sleepButton.disabled = false;
    setText(ui.sleepButton, quiet ? "唤醒" : "安静一下");
    ui.sleepButton.setAttribute("aria-pressed", String(quiet));
    ui.sleepButton.setAttribute("aria-label", quiet ? "唤醒陪伴角色，不播放欢迎语" : "让陪伴角色安静下来");
    ui.sleepButton.setAttribute("title", quiet ? "点击唤醒；不会播放欢迎语" : "点击后暂不听取语音，也不会自主发声");
    ui.sleepButton.classList.toggle("is-quiet", quiet);
    ui.nuanyuCharacter.setAttribute("aria-disabled", String(sleeping));
    ui.nuanyuCharacter.setAttribute("aria-label", sleeping ? `${currentRoleName}休息中` : `${currentRoleName}，点击让它动一动`);
    if (sleeping) clearPlayfulMotion();
    document.querySelector(".companion-panel").classList.toggle("is-sleeping", sleeping);
    const presenceText = sleeping ? "睡眠中。" : (status.visual_state === "PRESENT" ? "我在。" : "安静待着。");
    setText(ui.presencePill.querySelector("span:last-child"), presenceText);
    applyVisualState(status);
  }

  function renderMicControl(status) {
    if (!status || !status.asr) return;
    const dbg = status.asr._dbg || {};
    const manualMuted = Boolean(dbg.manual_mic_muted || status.asr.manual_muted);
    const systemMuted = Boolean(dbg.system_speaking);
    const stateText = manualMuted ? "已静音（ASR 常驻）" : (systemMuted ? "播放中暂时静音" : "已开启");
    if (ui.micMuteToggle) {
      ui.micMuteToggle.textContent = manualMuted ? "打开麦克风" : "静音麦克风";
      ui.micMuteToggle.setAttribute("aria-pressed", String(manualMuted));
      ui.micMuteToggle.setAttribute("title", manualMuted ? "打开板卡麦克风" : "静音板卡麦克风");
    }
    setText(ui.micState, stateText);
  }

  function renderQuietMode(status) {
    if (!ui.sleepButton || !status) return;
    const quiet = Boolean(status.quiet_mode || status.assistant_sleeping);
    ui.sleepButton.classList.toggle("is-quiet", quiet);
  }

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return response.json();
  }

  async function refreshStatus() {
    try {
      const status = await fetchJson("/api/status", { cache: "no-store" });
      renderStatus(status);
    } catch (error) {
      transient.fetchError = true;
      if (currentStatus) applyVisualState(currentStatus);
      setText(ui.systemSummary, "暂时没有连上暖语服务 · 查看详情");
      setText(ui.healthMessage, "页面暂时没有收到暖语服务的状态，请检查板卡服务是否仍在运行。");
      ui.healthMessage.classList.add("warning");
      console.warn("[Nuanyu] status refresh temporarily failed", error);
    } finally {
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(refreshStatus, document.hidden ? 4000 : 900);
    }
  }

  function optimisticMessage(role, text) {
    if (!currentStatus) return;
    const history = Array.isArray(currentStatus.chat_history) ? currentStatus.chat_history.slice() : [];
    history.push({ role, text, time: new Date().toLocaleTimeString("zh-CN", { hour12: false }), card_type: "normal" });
    currentStatus.chat_history = history;
    chatSignature = "";
    renderChat(history);
  }

  async function sendChat() {
    if (currentStatus && currentStatus.assistant_sleeping) return;
    const text = ui.chatInput.value.trim();
    if (!text || transient.pending) return;
    ui.chatInput.value = "";
    optimisticMessage("user", text);
    transient.pending = true;
    ui.sendButton.disabled = true;
    if (currentStatus) applyVisualState(currentStatus);
    try {
      const result = await fetchJson("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text })
      });
      if (result.status) renderStatus(result.status);
      else await refreshStatus();
    } catch (error) {
      console.error("[Nuanyu] chat failed", error);
      optimisticMessage("assistant", "我刚才没有接住这句话。你可以再说一次，我还在这里。");
    } finally {
      transient.pending = false;
      ui.sendButton.disabled = false;
      if (currentStatus) applyVisualState(currentStatus);
      ui.chatInput.focus();
    }
  }

  async function sendCommand(command, payload) {
    if (currentStatus && currentStatus.assistant_sleeping && !["sleep", "quiet_toggle"].includes(command)) return;
    if (transient.pending) return;
    transient.pending = true;
    if (currentStatus) applyVisualState(currentStatus);
    try {
      const result = await fetchJson("/api/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ command }, payload || {}))
      });
      if (result.status) renderStatus(result.status);
      else await refreshStatus();
    } catch (error) {
      console.error(`[Nuanyu] command ${command} failed`, error);
    } finally {
      transient.pending = false;
      if (currentStatus) applyVisualState(currentStatus);
    }
  }

  async function setMood(mood) {
    if (currentStatus && currentStatus.assistant_sleeping) return;
    if (!mood) return;
    setMoodButtons(mood);
    if (currentStatus) {
      currentStatus.current_mood = mood;
      applyVisualState(currentStatus);
    }
    try {
      const result = await fetchJson("/api/mood", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mood })
      });
      if (result.status) renderStatus(result.status);
      else await refreshStatus();
    } catch (error) {
      console.error("[Nuanyu] mood update failed", error);
    }
  }

  function openStudyModal() {
    if (currentStatus && currentStatus.assistant_sleeping) return;
    ui.studyModalMask.classList.add("visible");
    ui.goalInput.value = "";
    setTimeout(() => ui.goalInput.focus(), 30);
  }
  function closeStudyModal() { ui.studyModalMask.classList.remove("visible"); }
  function startStudyWithGoal() {
    const goal = ui.goalInput.value.trim() || "专注学习";
    closeStudyModal();
    sendCommand("study", { goal });
  }

  function initRecognition() {
    if (recognition) return true;
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) {
      ui.recordingStatus.textContent = "这个浏览器暂时不能直接听见你，可以先打字和我说话。";
      ui.recordingStatus.classList.add("visible");
      return false;
    }
    recognition = new Recognition();
    recognition.lang = "zh-CN";
    recognition.interimResults = true;
    recognition.continuous = true;
    recognition.onresult = (event) => {
      let text = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) text += event.results[index][0].transcript;
      ui.chatInput.value = text;
      ui.recordingStatus.textContent = text ? `我听到：${text}` : "我在听……";
    };
    recognition.onerror = (event) => {
      console.warn("[Nuanyu] speech recognition", event.error);
      ui.recordingStatus.textContent = event.error === "no-speech" ? "刚才没有听清，再试一次也可以。" : "语音暂时没接上，可以继续打字。";
      stopRecording(false);
    };
    recognition.onend = () => {
      if (recording) {
        try { recognition.start(); } catch (error) { console.debug(error); }
      }
    };
    return true;
  }

  function startRecording() {
    if (currentStatus && currentStatus.assistant_sleeping) return;
    if (transient.pending || !initRecognition()) return;
    recording = true;
    transient.listening = true;
    ui.micBtn.classList.add("recording");
    ui.micBtn.setAttribute("aria-label", "停止语音输入");
    ui.micBtn.setAttribute("title", "结束录音并发送");
    setText(ui.micBtn.querySelector(".voice-button-label"), "结束录音");
    ui.recordingStatus.textContent = "我在听，你慢慢说。说完再点一次。";
    ui.recordingStatus.classList.add("visible");
    if (currentStatus) applyVisualState(currentStatus);
    try { recognition.start(); } catch (error) { console.debug(error); }
  }

  function stopRecording(shouldSend) {
    recording = false;
    transient.listening = false;
    ui.micBtn.classList.remove("recording");
    ui.micBtn.setAttribute("aria-label", "开始语音输入");
    ui.micBtn.setAttribute("title", "启动语音识别");
    setText(ui.micBtn.querySelector(".voice-button-label"), "语音输入");
    try { if (recognition) recognition.stop(); } catch (error) { console.debug(error); }
    if (currentStatus) applyVisualState(currentStatus);
    const hasText = ui.chatInput.value.trim().length > 0;
    if (shouldSend && hasText) sendChat();
    else setTimeout(() => ui.recordingStatus.classList.remove("visible"), 2200);
  }

  function setupDebugControls() {
    const params = new URLSearchParams(window.location.search);
    if (!params.has("debug")) return;
    const requestedState = params.get("state");
    if (window.NuanyuVisualState.VALID_STATES.has(requestedState)) transient.debugState = requestedState;
    ui.debugControls.classList.add("visible");
    const title = document.createElement("div");
    title.textContent = "视觉状态调试";
    ui.debugControls.appendChild(title);
    window.NuanyuVisualState.VALID_STATES.forEach((state) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = state;
      button.addEventListener("click", () => {
        transient.debugState = transient.debugState === state ? null : state;
        if (currentStatus) applyVisualState(currentStatus);
      });
      ui.debugControls.appendChild(button);
    });
  }

  /* ── Settings Panel ────────────────────────────────── */

  const PERSONA_LABELS = { "default": "温柔陪伴", "calm_boy": "沉稳理性", "gentle_girl": "温暖活泼", "custom": "自定义" };

  async function fetchPersona() {
    try {
      return await fetchJson("/api/persona", { cache: "no-store" });
    } catch (e) { console.debug("[Nuanyu] persona fetch skipped", e.message); return null; }
  }

  function splitCatchphrases(value) {
    return String(value || "").split(/[\n,，;；]+/)
      .map(function (item) { return item.trim(); })
      .filter(Boolean).slice(0, 5);
  }

  async function savePersonaSettings() {
    const name = safeText(ui.roleNameInput.value, "");
    if (!name) {
      setText(ui.roleNameHint, "请输入角色名称");
      return;
    }
    const personality = safeText(ui.personalityInput.value, "");
    const languageStyle = safeText(ui.languageStyleInput.value, "");
    if (!personality || !languageStyle) {
      setText(ui.roleNameHint, "请填写性格和说话风格");
      return;
    }
    ui.saveRoleNameButton.disabled = true;
    setText(ui.roleNameHint, "正在保存角色设定…");
    try {
      const result = await fetchJson("/api/persona/update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          persona_id: currentPersonaId,
          name: name,
          personality: personality,
          language_style: languageStyle,
          catchphrases: splitCatchphrases(ui.catchphrasesInput.value),
        }),
      });
      applyRoleName(result.role_name || name);
      personaFormDirty = false;
      renderSettingPersona(result.persona || {});
      setText(ui.roleNameHint, "角色设定已保存，下一次对话立即生效");
      if (currentStatus) currentStatus.assistant_name = currentRoleName;
    } catch (error) {
      setText(ui.roleNameHint, "保存失败：" + error.message);
    } finally {
      ui.saveRoleNameButton.disabled = false;
    }
  }

  async function fetchMemoryStats() {
    try {
      return await fetchJson("/api/memory/stats", { cache: "no-store" });
    } catch (e) { console.debug("[Nuanyu] memory stats skipped", e.message); return null; }
  }

  async function fetchProactiveStatus() {
    try {
      return await fetchJson("/api/proactive/status", { cache: "no-store" });
    } catch (e) { console.debug("[Nuanyu] proactive status skipped", e.message); return null; }
  }

  async function updateProactiveConfig(enabled, level) {
    try {
      const result = await fetchJson("/api/proactive/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ enabled: enabled }, level == null ? {} : { level: Number(level) })),
      });
      return result;
    } catch (e) { console.warn("[Nuanyu] proactive config failed", e); return null; }
  }

  function renderSettingPersona(persona, presets) {
    (presets || []).forEach(function (preset) {
      if (preset && preset.persona_id) personaPresetsById[preset.persona_id] = preset;
    });
    const id = (persona && persona.persona_id) || "default";
    currentPersonaId = id;
    const label = PERSONA_LABELS[id] || "自定义";
    const roleName = safeText(persona && persona.name, currentRoleName);
    setText(ui.currentPersona, roleName + " · " + label);
    document.querySelectorAll("#characterPresetSelector [data-persona]").forEach(function (btn) {
      btn.setAttribute("aria-pressed", String(btn.dataset.persona === id));
    });
    const personaFields = [ui.personalityInput, ui.languageStyleInput, ui.catchphrasesInput];
    if (!personaFormDirty && !personaFields.includes(document.activeElement)) {
      ui.personalityInput.value = safeText(persona && persona.personality, "");
      ui.languageStyleInput.value = safeText(persona && persona.language_style, "");
      ui.catchphrasesInput.value = Array.isArray(persona && persona.catchphrases)
        ? persona.catchphrases.join("，") : "";
    }
  }

  function renderSettingMemory(stats) {
    if (stats) {
      setText(ui.memoryCount, String(stats.memory_count != null ? stats.memory_count : "—"));
      setText(ui.messageCount, String(stats.message_count != null ? stats.message_count : "—"));
    }
  }

  function renderSettingProactive(status) {
    if (!status) return;
    var enabled = Boolean(status.enabled);
    var label = enabled ? (status.level ? "已开启 (Lv." + status.level + ")" : "已开启") : "已关闭";
    setText(ui.proactiveStatusLabel, label);
    if (ui.proactiveToggle) {
      ui.proactiveToggle.checked = enabled;
      ui.proactiveToggle.disabled = false;
    }
    if (ui.proactiveLevel) {
      var level = Math.max(1, Math.min(10, Number(status.level || 5)));
      ui.proactiveLevel.value = String(level);
      setText(ui.proactiveLevelValue, "Lv." + level);
    }
  }

  async function setBoardMicMute(muted) {
    if (!ui.micMuteToggle) return;
    ui.micMuteToggle.disabled = true;
    try {
      const result = await fetchJson("/api/mic_control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ muted: Boolean(muted) }),
      });
      if (result.status) renderStatus(result.status);
      else await refreshStatus();
    } catch (error) {
      setText(ui.micState, "控制失败");
      console.warn("[Nuanyu] board mic control failed", error);
    } finally {
      ui.micMuteToggle.disabled = false;
    }
  }

  function renderSettingVoice() {
    var mode = ui.ttsModeSelect ? ui.ttsModeSelect.value : "drizzle";
    if (mode === "stream") {
      setText(ui.currentVoice, currentStreamVoiceLabel ? "Stream · " + currentStreamVoiceLabel : "Stream · 正在读取音色");
      fetchStreamVoices();
      return;
    }
    setText(ui.currentVoice, mode === "surge" ? "Surge · 自定义音色" : "Drizzle · 本地音色");
  }

  /* ── Stream / Doubao Voice Selector ───────────────────── */

  function toggleStreamVoiceGroup() {
    var mode = ui.ttsModeSelect ? ui.ttsModeSelect.value : "drizzle";
    var isStream = mode === "stream";
    if (ui.streamVoiceGroup) ui.streamVoiceGroup.style.display = isStream ? "" : "none";
    if (ui.surgeVoiceSettingsGroup) ui.surgeVoiceSettingsGroup.style.display = mode === "surge" ? "" : "none";
    if (isStream) fetchStreamVoices();
  }

  async function fetchStreamVoices() {
    if (streamVoicesLoading) return;
    streamVoicesLoading = true;
    try {
      var data = await fetchJson("/api/stream/voices", { cache: "no-store" });
      if (!data || !data.ok) {
        throw new Error((data && data.error) || "音色列表不可用");
      }
      renderStreamVoices(data);
    } catch (e) {
      if (ui.streamPreviewHint) ui.streamPreviewHint.textContent = "音色读取失败";
      console.warn("[Nuanyu] stream voices fetch failed", e.message);
    } finally {
      streamVoicesLoading = false;
    }
  }

  function renderStreamVoices(data) {
    var voices = data.voices || [];
    var current = (data.current && data.current.voice_type) || "";
    currentStreamVoiceLabel = (data.current && data.current.label) || current;
    ui.streamVoiceSelector.innerHTML = "";
    if (!voices.length) {
      ui.streamVoiceSelector.textContent = "暂无可用豆包音色";
      ui.streamPreviewBtn.disabled = true;
      return;
    }
    voices.forEach(function (v) {
      var btn = document.createElement("button");
      btn.className = "persona-btn";
      btn.type = "button";
      btn.dataset.voiceType = v.voice_type;
      btn.textContent = v.label;
      btn.setAttribute("aria-pressed", String(v.voice_type === current));
      btn.addEventListener("click", function () {
        setStreamVoice(v.voice_type, v.label);
      });
      ui.streamVoiceSelector.appendChild(btn);
    });
    // Update current voice display if Stream is active
    if (ui.ttsModeSelect && ui.ttsModeSelect.value === "stream" && current) {
      setText(ui.currentVoice, "Stream · " + currentStreamVoiceLabel);
    }
    ui.streamPreviewHint.textContent = "";
    ui.streamPreviewBtn.disabled = false;
  }

  async function setStreamVoice(voiceType, label) {
    currentStreamVoiceLabel = label;
    document.querySelectorAll("#streamVoiceSelector .persona-btn").forEach(function (b) {
      b.setAttribute("aria-pressed", "false");
    });
    var target = ui.streamVoiceSelector.querySelector("[data-voice-type=\"" + voiceType + "\"]");
    if (target) target.setAttribute("aria-pressed", "true");
    setText(ui.currentVoice, "Stream · " + label);
    ui.streamPreviewBtn.disabled = true;
    ui.streamPreviewHint.textContent = "切换中…";
    try {
      var result = await fetchJson("/api/stream/voice", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voice_type: voiceType }),
      });
      ui.streamPreviewBtn.disabled = false;
      if (result && result.ok) {
        currentStreamVoiceLabel = (result.current && result.current.label) || label;
        setText(ui.currentVoice, "Stream · " + currentStreamVoiceLabel);
        ui.streamPreviewHint.textContent = "已切换";
        setTimeout(function () { ui.streamPreviewHint.textContent = ""; }, 2000);
      } else {
        ui.streamPreviewHint.textContent = "失败: " + ((result && result.error) || "unknown");
        await fetchStreamVoices(); // revert
      }
    } catch (e) {
      ui.streamPreviewBtn.disabled = false;
      ui.streamPreviewHint.textContent = "网络错误";
      await fetchStreamVoices();
    }
  }

  function previewStreamVoice() {
    if (ui.streamPreviewBtn.disabled) return;
    ui.streamPreviewBtn.disabled = true;
    ui.streamPreviewHint.textContent = "试听中…";
    fetch("/api/stream/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    }).then(function (r) { return r.json(); })
      .then(function (data) {
        if (data && data.ok) {
          ui.streamPreviewHint.textContent = "正在播放";
          setTimeout(function () { ui.streamPreviewHint.textContent = ""; ui.streamPreviewBtn.disabled = false; }, 4000);
        } else {
          ui.streamPreviewHint.textContent = "失败";
          ui.streamPreviewBtn.disabled = false;
        }
      }).catch(function () {
        ui.streamPreviewHint.textContent = "错误";
        ui.streamPreviewBtn.disabled = false;
      });
  }

  async function refreshSettings() {
    try {
      var persona = await fetchPersona();
      if (persona) {
        applyRoleName(persona.role_name || (persona.active && persona.active.name));
        renderSettingPersona(persona.active || persona.persona || persona, persona.presets || []);
      }

      var memStats = await fetchMemoryStats();
      if (memStats) renderSettingMemory(memStats.memory || memStats);

      var proactive = await fetchProactiveStatus();
      if (proactive) renderSettingProactive(proactive.proactive || proactive.status || proactive);

      renderSettingVoice();
    } catch (e) { /* silent — settings are supplementary */ }
  }

  function bindSettingsEvents() {
    ui.saveRoleNameButton.addEventListener("click", savePersonaSettings);
    ui.roleNameInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") savePersonaSettings();
    });
    document.querySelectorAll("#characterPresetSelector [data-persona]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var personaId = btn.dataset.persona;
        var preset = personaPresetsById[personaId];
        document.querySelectorAll("#characterPresetSelector [data-persona]").forEach(function (b) {
          b.setAttribute("aria-pressed", "false");
        });
        btn.setAttribute("aria-pressed", "true");
        currentPersonaId = personaId;
        personaFormDirty = true;
        if (preset) {
          ui.personalityInput.value = preset.personality || "";
          ui.languageStyleInput.value = preset.language_style || "";
          ui.catchphrasesInput.value = (preset.catchphrases || []).join("，");
        }
        setText(ui.currentPersona, currentRoleName + " · " + (PERSONA_LABELS[personaId] || "自定义"));
        setText(ui.roleNameHint, "模板已填入，保存后生效");
      });
    });
    [ui.roleNameInput, ui.personalityInput, ui.languageStyleInput, ui.catchphrasesInput].forEach(function (field) {
      field.addEventListener("input", function () {
        personaFormDirty = true;
        setText(ui.roleNameHint, "有尚未保存的角色设定");
      });
    });

    ui.proactiveToggle.addEventListener("change", async function () {
      var enabled = ui.proactiveToggle.checked;
      ui.proactiveToggle.disabled = true;
      var result = await updateProactiveConfig(enabled, ui.proactiveLevel && ui.proactiveLevel.value);
      ui.proactiveToggle.disabled = false;
      if (result && (result.proactive || result.status)) {
        renderSettingProactive(result.proactive || result.status);
      }
      else {
        ui.proactiveToggle.checked = !enabled; // revert on failure
        await refreshSettings();
      }
    });
    if (ui.micMuteToggle) {
      ui.micMuteToggle.addEventListener("click", function () {
        var dbg = currentStatus && currentStatus.asr ? (currentStatus.asr._dbg || {}) : {};
        var muted = Boolean(dbg.manual_mic_muted || (currentStatus && currentStatus.asr && currentStatus.asr.manual_muted));
        setBoardMicMute(!muted);
      });
    }
    if (ui.proactiveLevel) {
      ui.proactiveLevel.addEventListener("input", function () {
        setText(ui.proactiveLevelValue, "Lv." + ui.proactiveLevel.value);
      });
      ui.proactiveLevel.addEventListener("change", async function () {
        var result = await updateProactiveConfig(ui.proactiveToggle.checked, ui.proactiveLevel.value);
        if (result && (result.proactive || result.status)) {
          renderSettingProactive(result.proactive || result.status);
        }
      });
    }

    // Stream voice selector visibility triggered by TTS mode change
    if (ui.ttsModeSelect) {
      ui.ttsModeSelect.addEventListener("change", toggleStreamVoiceGroup);
    }

    // Stream preview button
    ui.streamPreviewBtn.addEventListener("click", previewStreamVoice);
    if (ui.manageSurgeVoiceButton) {
      ui.manageSurgeVoiceButton.addEventListener("click", async function () {
        if (typeof setTtsBackend === "function" && ui.ttsModeSelect.value !== "surge") {
          await setTtsBackend("surge");
        }
        var drawer = document.getElementById("surgeVoiceDrawer");
        if (drawer) drawer.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
    }
  }

  function bindEvents() {
    const companionTools = document.querySelector(".companion-tools");
    const companionPanel = document.querySelector(".companion-panel");
    ui.sendButton.addEventListener("click", sendChat);
    ui.chatInput.addEventListener("keydown", (event) => { if (event.key === "Enter") sendChat(); });
    ui.micBtn.addEventListener("click", () => { if (recording) stopRecording(true); else startRecording(); });
    ui.nuanyuCharacter.addEventListener("click", playRandomMotion);
    ui.nuanyuCharacter.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        playRandomMotion();
      }
    });
    ui.sleepButton.addEventListener("click", () => {
      if (recording) stopRecording(false);
      closeStudyModal();
      sendCommand("quiet_toggle");
    });
    document.querySelectorAll(".mood-button").forEach((button) => button.addEventListener("click", () => setMood(button.dataset.mood)));
    ui.focusAction.addEventListener("click", () => { if (currentStatus && currentStatus.study_running) sendCommand("stop"); else openStudyModal(); });
    ui.chatAction.addEventListener("click", () => {
      if (companionTools) companionTools.open = false;
      ui.chatInput.focus();
    });
    ui.todayAction.addEventListener("click", () => { ui.systemDetails.open = true; ui.systemDetails.scrollIntoView({ behavior: "smooth", block: "nearest" }); });
    ui.moodAction.addEventListener("click", () => { ui.moodDock.scrollIntoView({ behavior: "smooth", block: "center" }); ui.moodDock.querySelector("button").focus(); });
    ui.cancelStudyButton.addEventListener("click", closeStudyModal);
    ui.startStudyButton.addEventListener("click", startStudyWithGoal);
    ui.goalInput.addEventListener("keydown", (event) => { if (event.key === "Enter") startStudyWithGoal(); });
    ui.studyModalMask.addEventListener("click", (event) => { if (event.target === ui.studyModalMask) closeStudyModal(); });
    if (companionTools) companionTools.addEventListener("toggle", () => {
      if (companionPanel) companionPanel.classList.toggle("settings-open", companionTools.open);
      if (companionTools.open) {
        const drawer = companionTools.querySelector(".tools-drawer");
        if (drawer) drawer.scrollTop = 0;
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      closeStudyModal();
      if (companionTools) companionTools.open = false;
    });
    document.addEventListener("visibilitychange", () => {
      document.body.classList.toggle("page-hidden", document.hidden);
      if (!document.hidden) { refreshStatus(); scheduleIdleDrift(120); }
    });
    window.addEventListener("pagehide", () => {
      if (recording) stopRecording(false);
      clearTimeout(refreshTimer);
      clearTimeout(sensorTimer);
      clearTimeout(settingsTimer);
      clearTimeout(idleDriftTimer);
    });
    bindSettingsEvents();
  }

  async function doLogout() {
    if (!window.confirm("确定要切换账号吗？当前对话会保留。")) return;
    await fetch("/api/logout");
    window.location.reload();
  }
  async function shutdownNuanyu() {
    if (!window.confirm(`确定要让${currentRoleName}暂时休息吗？`)) return;
    await fetch("/api/shutdown", { method: "POST" });
    window.alert(`${currentRoleName}已经休息了。`);
  }

  window.sendCommand = sendCommand;
  window.doLogout = doLogout;
  window.shutdownNuanyu = shutdownNuanyu;
  window.refreshStreamVoices = fetchStreamVoices;

  cacheElements();
  bindEvents();
  setupDebugControls();
  scheduleIdleDrift(180);
  refreshStatus();
  refreshSensors();
  toggleStreamVoiceGroup();

  /* settings poll — slower cadence, supplementary to main status */
  (function scheduleSettings() {
    refreshSettings().finally(function () {
      clearTimeout(settingsTimer);
      settingsTimer = setTimeout(scheduleSettings, 8000);
    });
  })();
})();


// ── Surge voice management and unified backend selector ──

let _currentBackend = "drizzle";

async function surgeFetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${response.status} ${response.statusText}`);
  return payload;
}

function setSurgeServerStatus(online, text) {
  const status = document.getElementById("surgeServerStatus");
  if (!status) return;
  const dot = status.querySelector(".surge-dot");
  const label = status.querySelector("span:last-child");
  dot.classList.toggle("online", Boolean(online));
  dot.classList.toggle("offline", !online);
  label.textContent = text;
}

function syncBackendUI(backend) {
  _currentBackend = backend;
  const headerSelect = document.getElementById("ttsModeSelect");
  const settingsSelect = document.getElementById("ttsBackendSelect");
  if (headerSelect) headerSelect.value = backend;
  if (settingsSelect) settingsSelect.value = backend;

  const streamGroup = document.getElementById("streamVoiceGroup");
  if (streamGroup) streamGroup.style.display = backend === "stream" ? "" : "none";
  const surgeSettingsGroup = document.getElementById("surgeVoiceSettingsGroup");
  if (surgeSettingsGroup) surgeSettingsGroup.style.display = backend === "surge" ? "" : "none";
  const currentVoice = document.getElementById("currentVoice");
  if (currentVoice && backend === "drizzle") currentVoice.textContent = "Drizzle · 本地音色";
  if (currentVoice && backend === "surge") currentVoice.textContent = "Surge · 自定义音色";
  if (currentVoice && backend === "stream" && !currentVoice.textContent.startsWith("Stream")) {
    currentVoice.textContent = "Stream · 正在读取音色";
  }
  if (backend === "stream" && typeof window.refreshStreamVoices === "function") {
    window.refreshStreamVoices();
  }

  const drawer = document.getElementById("surgeVoiceDrawer");
  if (drawer) drawer.hidden = backend !== "surge";
  if (backend === "surge") loadSurgeVoices();
}

async function setTtsBackend(backend) {
  const previous = _currentBackend;
  syncBackendUI(backend);
  try {
    const resp = await surgeFetchJson("/api/tts_provider", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ backend })
    });
    if (!resp.ok) throw new Error(resp.message || "切换失败");
    syncBackendUI(resp.current || backend);
  } catch (error) {
    syncBackendUI(previous);
    window.alert("声音切换失败：" + error.message);
  }
}

function openSurgeUpload() {
  document.getElementById("surgeUploadModal").style.display = "flex";
  document.getElementById("surgeUploadName2").value = "";
  document.getElementById("surgeUploadFile").value = "";
  document.getElementById("surgeUploadText2").value = "";
  document.getElementById("surgeUploadStatus").textContent = "";
}

function closeSurgeUpload() {
  document.getElementById("surgeUploadModal").style.display = "none";
}

function createSurgeVoiceCard(voice, activeId) {
  const active = voice.voice_id === activeId;
  const card = document.createElement("div");
  card.className = "surge-voice-card" + (active ? " active" : "");

  const info = document.createElement("div");
  info.className = "surge-voice-info";
  const name = document.createElement("strong");
  name.textContent = voice.name || "未命名音色";
  const meta = document.createElement("small");
  meta.textContent = `${Number(voice.duration || 0).toFixed(1)} 秒参考音频`;
  info.append(name, meta);

  const actions = document.createElement("div");
  actions.className = "surge-voice-actions";
  if (active) {
    const badge = document.createElement("span");
    badge.className = "surge-badge-active";
    badge.textContent = "正在使用";
    actions.appendChild(badge);
  } else {
    const activate = document.createElement("button");
    activate.type = "button";
    activate.className = "surge-btn-activate";
    activate.textContent = "使用";
    activate.addEventListener("click", () => activateSurgeVoice(voice.voice_id));
    actions.appendChild(activate);
  }
  if (voice.preview_available) {
    const preview = document.createElement("button");
    preview.type = "button";
    preview.textContent = "试听";
    preview.addEventListener("click", () => previewSurgeVoice(voice.voice_id, preview));
    actions.appendChild(preview);
  }
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "surge-btn-delete";
  remove.textContent = "删除";
  remove.disabled = active;
  remove.title = active ? "请先切换到其他音色" : "删除此音色";
  remove.addEventListener("click", () => deleteSurgeVoice(voice.voice_id));
  actions.appendChild(remove);

  card.append(info, actions);
  return card;
}

async function loadSurgeVoices() {
  const list = document.getElementById("surgeVoiceList");
  if (!list) return;
  try {
    const [resp, health] = await Promise.all([
      surgeFetchJson("/api/surge/voices", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      }),
      surgeFetchJson("/api/surge/health", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      }).catch(() => null)
    ]);
    const voices = resp.voices || [];
    const activeId = resp.active_voice_id;
    const activeVoice = voices.find((voice) => voice.voice_id === activeId);
    document.getElementById("surgeVoiceCount").textContent = String(voices.length);
    document.getElementById("surgeActiveVoice").textContent =
      activeVoice ? `当前：${activeVoice.name}` : "请选择一个自定义音色";
    list.replaceChildren();
    if (!voices.length) {
      const empty = document.createElement("div");
      empty.className = "surge-empty";
      empty.innerHTML = "<p>还没有自定义音色。</p><p class=\"surge-hint\">上传一段清晰语音，就能创建新声音。</p>";
      list.appendChild(empty);
    } else {
      voices.forEach((voice) => list.appendChild(createSurgeVoiceCard(voice, activeId)));
    }
    const online = Boolean(health && health.ok && health.health && health.health.server_available);
    setSurgeServerStatus(online, online ? "Surge 服务在线" : "Surge 服务暂时离线");
  } catch (error) {
    list.innerHTML = "<div class=\"surge-empty\"><p>音色暂时加载失败。</p></div>";
    setSurgeServerStatus(false, error.message);
  }
}

async function activateSurgeVoice(voiceId) {
  try {
    await surgeFetchJson("/api/surge/voices/" + encodeURIComponent(voiceId) + "/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    });
    await loadSurgeVoices();
  } catch (error) {
    window.alert("启用音色失败：" + error.message);
  }
}

async function previewSurgeVoice(voiceId, button) {
  const previousText = button.textContent;
  button.disabled = true;
  button.textContent = "加载中";
  try {
    const resp = await surgeFetchJson("/api/surge/voices/" + encodeURIComponent(voiceId) + "/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    });
    if (resp.output === "board") return;
    if (!resp.wav_b64) throw new Error("没有可试听的音频");
    await new Audio("data:audio/wav;base64," + resp.wav_b64).play();
  } catch (error) {
    window.alert("试听失败：" + error.message);
  } finally {
    button.disabled = false;
    button.textContent = previousText;
  }
}

async function deleteSurgeVoice(voiceId) {
  if (!window.confirm("确定删除这个自定义音色吗？")) return;
  try {
    await surgeFetchJson("/api/surge/voices/" + encodeURIComponent(voiceId) + "/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    });
    await loadSurgeVoices();
  } catch (error) {
    window.alert("删除音色失败：" + error.message);
  }
}

async function doSurgeUpload() {
  const nameEl = document.getElementById("surgeUploadName2");
  const fileEl = document.getElementById("surgeUploadFile");
  const textEl = document.getElementById("surgeUploadText2");
  const statusEl = document.getElementById("surgeUploadStatus");
  const name = nameEl.value.trim();
  const referenceText = textEl.value.trim();
  const file = fileEl.files[0];
  if (!name) { statusEl.textContent = "请输入音色名称"; return; }
  if (!file) { statusEl.textContent = "请选择音频文件"; return; }
  if (!referenceText) { statusEl.textContent = "请输入参考文本"; return; }
  statusEl.textContent = "正在创建音色…";
  try {
    const reader = new FileReader();
    const audioB64 = await new Promise((resolve, reject) => {
      reader.onload = () => resolve(reader.result.split(",")[1]);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
    const resp = await surgeFetchJson("/api/surge/upload", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        audio_b64: audioB64,
        reference_text: referenceText,
        filename: file.name
      })
    });
    if (!resp.ok) throw new Error(resp.error || "创建失败");
    statusEl.textContent = "创建成功";
    setTimeout(() => {
      closeSurgeUpload();
      loadSurgeVoices();
    }, 500);
  } catch (error) {
    statusEl.textContent = "创建失败：" + error.message;
  }
}

async function initSurgeUI() {
  const headerSelect = document.getElementById("ttsModeSelect");
  const settingsSelect = document.getElementById("ttsBackendSelect");
  [headerSelect, settingsSelect].forEach((select) => {
    if (select) select.addEventListener("change", () => setTtsBackend(select.value));
  });

  document.getElementById("surgeUploadBtn")?.addEventListener("click", openSurgeUpload);
  document.getElementById("surgeUploadConfirm")?.addEventListener("click", doSurgeUpload);
  document.getElementById("surgeDrawerToggle")?.addEventListener("click", function () {
    const body = document.getElementById("surgeVoiceGroup");
    const expanded = this.getAttribute("aria-expanded") === "true";
    this.setAttribute("aria-expanded", String(!expanded));
    body.hidden = expanded;
  });

  try {
    const resp = await surgeFetchJson("/api/tts_provider", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    });
    syncBackendUI(resp.current || "drizzle");
  } catch (error) {
    syncBackendUI("drizzle");
    console.warn("[Nuanyu] TTS backend state unavailable", error);
  }
}

window.openSurgeUpload = openSurgeUpload;
window.closeSurgeUpload = closeSurgeUpload;
document.addEventListener("DOMContentLoaded", initSurgeUI);
