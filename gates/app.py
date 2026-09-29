"""Flask HTTP layer for the gates service.

POST /api/evaluate  — validate + evaluate a gating request.
GET  /api/health    — liveness probe.

Any validation problem rejects the whole request with 422; nothing is
evaluated partially.
"""

from __future__ import annotations

from flask import Flask, jsonify, request

from gating import ValidationError, evaluate, validate_payload


def _validation_error(message):
    return (
        jsonify({"error": {"code": "VALIDATION_FAILED", "message": message}}),
        422,
    )


def create_app():
    app = Flask(__name__)
    # 5000 points + 20 gates of 12 vertices is far below this ceiling.
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/evaluate")
    def evaluate_endpoint():
        payload = request.get_json(silent=True)
        if payload is None:
            return _validation_error("request body must be valid JSON")
        try:
            points, gates = validate_payload(payload)
        except ValidationError as exc:
            return _validation_error(str(exc))
        return jsonify(evaluate(points, gates))

    @app.errorhandler(413)
    def too_large(_):
        return _validation_error("request body too large")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
