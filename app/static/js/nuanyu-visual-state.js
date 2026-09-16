(function () {
  "use strict";

  const VALID_STATES = new Set([
    "idle", "welcome", "listening", "thinking", "speaking", "happy",
    "comfort", "tired", "focus", "sleep", "error"
  ]);

  const STATE_COPY = {
    idle: "我在。慢慢说。",
    welcome: "你回来了。今天想先做什么？",
    listening: "我在听。",
    thinking: "让我想一想。",
    speaking: "我想这样说。",
    happy: "今天的状态不错。",
    comfort: "先不用急。我们只看眼前这一步。",
    tired: "今天可以做轻一点。",
    focus: "这一轮，我和你一起。",
    sleep: "小陪睡着了。",
    error: "有个功能暂时没接上，文字对话仍然可用。"
  };

  const STATE_LABEL = {
    idle: "我在", welcome: "你回来了", listening: "听着",
    thinking: "思考中", speaking: "回应中", happy: "状态不错",
    comfort: "慢一点", tired: "放轻一些", focus: "专注中",
    sleep: "睡眠中", error: "连接提示"
  };

  function textIncludes(value, words) {
    const text = String(value || "").toLowerCase();
    return words.some((word) => text.includes(word.toLowerCase()));
  }

  function collectIssues(status) {
    const pipeline = status.pipeline || {};
    const issues = [];
    if (textIncludes(pipeline.tts, ["失败", "离线", "错误", "队列失败"])) {
      issues.push("我的语音合成暂时没连上，不过你还可以打字和我说话。");
    }
    if (textIncludes(pipeline.speaker, ["失败", "离线", "错误"])) {
      issues.push("扬声器现在没有回应，文字陪伴仍然可以使用。");
    }
    if (textIncludes(pipeline.ai_reply, ["失败", "错误"])) {
      issues.push("我刚才没有想出回答，可以稍后再试一次。");
    }
    const asrState = String((status.asr || {}).state || "");
    if (textIncludes(asrState, ["error", "failed"])) {
      issues.push("我的语音暂时没连上，不过你还可以打字和我说话。");
    }
    return Array.from(new Set(issues));
  }

  function isSpeaking(status) {
    const pipeline = status.pipeline || {};
    const tts = status.tts || {};
    return textIncludes(pipeline.tts, ["排队中", "合成中", "播放中"])
      || textIncludes(pipeline.speaker, ["准备播放", "播放中"])
      || textIncludes(tts.state, ["speaking", "playing", "synthesizing"]);
  }

  function isThinking(status, transient) {
    return Boolean(transient.pending)
      || textIncludes((status.pipeline || {}).ai_reply, ["生成中", "思考中", "处理中"]);
  }

  function resolve(status, transient) {
    const safeStatus = status || {};
    const safeTransient = transient || {};
    const issues = collectIssues(safeStatus);
    let state = "idle";

    if (safeStatus.assistant_sleeping) state = "sleep";
    else if (safeTransient.fetchError || issues.length) state = "error";
    else if (isSpeaking(safeStatus)) state = "speaking";
    else if (safeTransient.listening) state = "listening";
    else if (isThinking(safeStatus, safeTransient)) state = "thinking";
    else if (safeStatus.study_running) state = "focus";
    else if (safeTransient.justReturned || safeStatus.return_prompt) state = "welcome";
    else if (safeStatus.current_mood === "开心") state = "happy";
    else if (safeStatus.current_mood === "疲惫") state = "tired";
    else if (["焦虑", "崩溃", "低落"].includes(safeStatus.current_mood)) state = "comfort";
    else if (safeStatus.visual_state === "AWAY" && safeTransient.awaySeconds >= 12) state = "sleep";

    if (!VALID_STATES.has(state)) state = "idle";
    const copy = issues[0] || STATE_COPY[state];
    return { state, copy, label: STATE_LABEL[state], issues };
  }

  window.NuanyuVisualState = {
    VALID_STATES,
    STATE_COPY,
    STATE_LABEL,
    collectIssues,
    resolve
  };
})();
