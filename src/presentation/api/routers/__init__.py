"""Aggregate API routers for presentation layer.

Importing the modules ensures Pylint can resolve the attribute instead of
attempting to find a top-level symbol during the "from X import router" form.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from .semesters import semester_router
from .subjects import router as subjects_router
from .assignments import router as assignments_router
from .exams import router as exams_router
from .courses import courses_router
from .export import router as export_router
from .import_data import router as import_router
from .user_courses import router as user_courses_router

api_router = APIRouter()
# Namespace each resource router to avoid path shadowing like /api/{id} catching /api/_health
api_router.include_router(semester_router, prefix="/semesters", tags=["Semesters"])
api_router.include_router(subjects_router, prefix="/subjects", tags=["Subjects"])
api_router.include_router(assignments_router, prefix="/assignments", tags=["Assignments"])
api_router.include_router(exams_router, prefix="/exams", tags=["Exams"])
api_router.include_router(export_router, prefix="/export", tags=["Export"])
api_router.include_router(import_router, prefix="/import", tags=["Import"])
api_router.include_router(user_courses_router)
api_router.include_router(courses_router)  # already has /courses prefix


@api_router.get("/_health")
def api_health() -> dict:
	"""Simple API health check."""
	return {"status": "ok"}


@api_router.get("/_debug")
def api_debug(request: Request) -> dict:
	"""Expose a small debug index of handy API endpoints and whether they are mounted.

	Useful to verify if a deployment includes recent routes without digging into code.
	"""
	# Gate debug endpoint behind env flag and optional token
	enabled = getattr(request.app.state, "enable_debug_routes", False)
	token = getattr(request.app.state, "debug_token", None)
	if not enabled:
		return {"detail": "Not found"}
	if token and request.query_params.get("token") != token:
		return {"detail": "Not found"}
	# Collect all route paths registered on the app
	paths: list[str] = []
	for r in request.app.routes:
		# FastAPI/Starlette versions differ in attribute naming
		p = getattr(r, "path", None) or getattr(r, "path_format", None)
		if isinstance(p, str):
			paths.append(p)
	def present(path: str) -> bool:
		"""Return whether the given path is present in the collected paths.

		Args:
			path: Path to check.

		Returns:
			True if the path exists in the routes list, False otherwise.
		"""
		return path in paths
	return {
		"routes_present": {
			"/api/courses": present("/api/courses"),
			"/api/courses/_codes": present("/api/courses/_codes"),
			"/api/courses/by-code/{code}": present("/api/courses/by-code/{code}"),
		},
		"hint": "If a route shows false, restart or rebuild the web container to pick up recent code.",
	}

__all__ = ["api_router"]
