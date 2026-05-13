from pipelines.agent_core import PDRAgentCore


def print_clarification(clarification: dict) -> None:
    print("\n==== 需要先确认地点 ====")
    print(clarification.get("prompt", "请先确认地点"))
    for slot in clarification.get("pending_slots", []):
        print(f"\n[{slot.get('slot_id')}] {slot.get('question')}")
        for idx, candidate in enumerate(slot.get("candidates", []), start=1):
            name = candidate.get("name", "")
            address = candidate.get("address", "")
            distance = candidate.get("distance_m")
            distance_text = f" | 距离 {distance} 米" if distance is not None else ""
            print(f"  {idx}. {name} | {address}{distance_text}")


def collect_poi_selections(clarification: dict) -> dict:
    selections: dict[str, int] = {}
    for slot in clarification.get("pending_slots", []):
        candidates = slot.get("candidates", [])
        if not candidates:
            continue
        while True:
            raw = input(f"请输入 [{slot.get('slot_id')}] 的序号 (1-{len(candidates)}): ").strip()
            if raw.isdigit():
                value = int(raw)
                if 1 <= value <= len(candidates):
                    selections[slot.get("slot_id")] = value
                    break
            print("输入无效，请重新输入。")
    return selections


def run_test():
    print("正在启动 MapAgent 动态 DAG 智能体...")
    agent = PDRAgentCore()

    user_voice = "我要去最近的烤肉店，顺便要途径咖啡店"
    current_gps = "113.963041,22.801446"

    print("=" * 60)
    print(f"用户语音: {user_voice}")
    print(f"后台定位: {current_gps}")
    print("=" * 60)

    poi_selections: dict[str, int] = {}
    for _ in range(3):
        report = agent.execute_dag_engine(
            user_query=user_voice,
            current_gps=current_gps,
            poi_selections=poi_selections or None,
        )
        if not (report and report.get("status") == "needs_clarification"):
            break
        print_clarification(report.get("clarification", {}))
        new_selections = collect_poi_selections(report.get("clarification", {}))
        poi_selections.update(new_selections)
    else:
        report = {"reason": "澄清轮次过多，已停止"}

    print("\n" + "=" * 20 + " 智能体执行结果 " + "=" * 20)
    if report and report.get("status") == "needs_clarification":
        print_clarification(report.get("clarification", {}))
        print("\n本轮仍需更多选择，请继续运行脚本。")
        return

    if report and report.get("distance_km") is not None:
        narration = agent.llm_client.generate_friendly_response_with_meta(user_voice, report)
        friendly_reply = narration["text"]
        narration_title = "AI 语音助理播报" if narration["source"] == "llm" else "本地兜底播报"

        print("\n" + "=" * 15 + f" {narration_title} " + "=" * 15)
        print(f"\n{friendly_reply}\n")

        print("======== 详细后台数据（供调试查看） ========")
        if report.get("weather"):
            print(f"目的地区域天气: {report.get('weather')}")
        print(f"路线总长: {report.get('distance_km')} 公里")
        print(f"预计耗时: {report.get('eta_minutes')} 分钟")
        print(f"红绿灯数: {report.get('traffic_lights', 0)} 个")
        print("前 3 步详细指引:")
        for i, step in enumerate(report.get("steps", [])[:3], start=1):
            print(f"  [{i}] {step}")
        print("  ...")
    else:
        reason = report.get("reason", "未知执行异常") if isinstance(report, dict) else "无返回"
        print(f"任务执行失败，原因: {reason}")


if __name__ == "__main__":
    run_test()
