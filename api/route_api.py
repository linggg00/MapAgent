from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from pipelines.agent_core import PDRAgentCore
from schemas.dag_model import RoutePlanResponse, StrategyFeedbackResponse
from schemas.request import RoutePlanRequest, StrategyFeedbackRequest
from services.reward_policy import RewardPolicyService


DEFAULT_AMAP_JS_API_KEY = "9a19873dd960531ad650176fa5f6e7d0"
WEATHER_KEYWORDS = (
    "天气",
    "下雨",
    "雨",
    "晴",
    "阴",
    "多云",
    "台风",
    "暴雨",
    "大雾",
    "雾",
    "雪",
    "温度",
    "气温",
)

router = APIRouter(prefix="/api/route", tags=["route"])
agent_core = PDRAgentCore()
reward_policy = RewardPolicyService()


class QuickRouteRequest(BaseModel):
    query: str = Field(..., min_length=1, description="用户在前端输入的自然语言需求")
    current_gps: str = Field(..., min_length=3, description="浏览器定位到的经纬度，格式 lng,lat")
    coord_system: str = Field(default="amap", description="amap 表示高德坐标，gps 表示浏览器原生 WGS84 坐标")
    poi_selections: dict[str, Any] | None = Field(default=None, description="POI 澄清选择")


class NormalizeLocationRequest(BaseModel):
    location: str = Field(..., min_length=3)
    coord_system: str = Field(default="gps")


def query_mentions_weather(query: str) -> bool:
    return any(keyword in query for keyword in WEATHER_KEYWORDS)


def resolve_weather_location(report: dict[str, Any]) -> str:
    pois = report.get("pois") or []
    for poi in pois:
        if isinstance(poi, dict) and poi.get("role") == "destination":
            return poi.get("location") or poi.get("name") or ""
    if isinstance(report.get("destination"), str):
        return report["destination"]
    return report.get("origin", "")


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "map-agent"}


@router.post("/plan", response_model=RoutePlanResponse)
async def plan_route(request: RoutePlanRequest) -> RoutePlanResponse:
    return await asyncio.to_thread(agent_core.execute, request)


@router.post("/quick")
async def quick_plan_route(request: QuickRouteRequest) -> dict[str, Any]:
    current_gps = request.current_gps
    if request.coord_system.lower() == "gps":
        current_gps = agent_core.amap_client.convert_coord_from_gps(current_gps)

    try:
        report = await asyncio.to_thread(
            agent_core.execute_dag_engine,
            request.query,
            current_gps,
            request.poi_selections,
        )
        if isinstance(report, dict):
            report["origin"] = report.get("origin") or current_gps
            report["origin_address"] = agent_core.amap_client.reverse_geocode(current_gps).get("formatted_address", "")
            if report.get("distance_km") is not None and query_mentions_weather(request.query):
                weather_location = resolve_weather_location(report)
                weather = "未知"
                if "," in weather_location:
                    weather = await asyncio.to_thread(agent_core.amap_client.get_weather_by_coord, weather_location)
                elif weather_location:
                    weather = await asyncio.to_thread(agent_core.amap_client.get_weather_by_location, weather_location)
                report["weather"] = weather
                report["weather_requested"] = True
            if report.get("distance_km") is not None and not report.get("narration"):
                try:
                    narration = await asyncio.to_thread(
                        agent_core.llm_client.generate_friendly_response_with_meta,
                        request.query,
                        report,
                    )
                    report["narration"] = narration["text"]
                    report["narration_source"] = narration["source"]
                    if narration.get("error"):
                        report["narration_error"] = narration["error"]
                except Exception as narration_exc:
                    report["narration"] = (
                        f"已为您规划路线：全程约 {report.get('distance_km')} 公里，"
                        f"预计 {report.get('eta_minutes')} 分钟，红绿灯约 {report.get('traffic_lights', 0)} 个。"
                        f"语音播报生成失败：{narration_exc}"
                    )
                    report["narration_source"] = "fallback"
        return report
    except Exception as exc:
        return {
            "status": "failed",
            "reason": f"路线规划服务异常: {exc}",
            "origin": current_gps,
            "query": request.query,
        }


@router.get("/map-config")
async def map_config() -> dict[str, str]:
    import os

    return {
        "amap_key": agent_core.amap_client.api_key or "",
        "amap_js_key": os.getenv("AMAP_JS_API_KEY", DEFAULT_AMAP_JS_API_KEY),
        "amap_security_js_code": os.getenv("AMAP_SECURITY_JS_CODE", ""),
    }


@router.post("/normalize-location")
async def normalize_location(request: NormalizeLocationRequest) -> dict[str, Any]:
    location = request.location
    if request.coord_system.lower() == "gps":
        location = agent_core.amap_client.convert_coord_from_gps(location)
    address = agent_core.amap_client.reverse_geocode(location)
    return {
        "location": location,
        "coord_system": "amap",
        "formatted_address": address.get("formatted_address", ""),
    }


@router.post("/feedback", response_model=StrategyFeedbackResponse)
async def apply_feedback(request: StrategyFeedbackRequest) -> StrategyFeedbackResponse:
    return await asyncio.to_thread(
        reward_policy.apply_feedback,
        request.context_key,
        request.strategy,
        request.feedback_score,
    )
