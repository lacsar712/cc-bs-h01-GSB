// 结论着色与展示：颜色/文案必须与库里最终结论同一意思。
// 合格 -> 绿(pass)，越界 -> 红(fail)，尚未判定 -> 黄(wait)。
export function verdictClass(verdict) {
  if (verdict === "合格") return "tag pass";
  if (verdict === "越界") return "tag fail";
  return "tag wait";
}

export function displayVerdict(row) {
  if (row.verdict) return row.verdict;
  if (row.status === "pending") return "待处理";
  if (row.status === "processing") return "处理中";
  return "—";
}
