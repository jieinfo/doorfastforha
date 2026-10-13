const messages = {
  en: {
    listen: "Listen", stopListen: "Stop listening", intercom: "Start intercom", stopIntercom: "Stop intercom",
    listenOnly: "Listening only", https: "HTTPS required", micDenied: "Microphone denied",
    noCall: "Call not established", station: "Station mismatch", busy: "Microphone busy",
    update: "Update Doorfast", lost: "Audio connection lost",
  },
  zh: {
    listen: "收听", stopListen: "停止收听", intercom: "开始对讲", stopIntercom: "停止对讲",
    listenOnly: "仅收听", https: "麦克风需要HTTPS", micDenied: "麦克风权限被拒绝",
    noCall: "通话尚未建立", station: "门口机不匹配", busy: "麦克风忙碌",
    update: "请更新 Doorfast", lost: "音频连接已断开",
  },
};

export function audioMessage(language, key) {
  const table = /^zh(?:-|$)/i.test(language ?? "") ? messages.zh : messages.en;
  if (key === "identity") return `${table.noCall} / ${table.station}`;
  if (key === "listenOnlyHttps" || key === "listenOnlyDenied") {
    return table.listenOnly + (table === messages.zh ? "：" : ": ") + table[key === "listenOnlyHttps" ? "https" : "micDenied"];
  }
  return table[key] ?? table.lost;
}
