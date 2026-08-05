from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.database import Base, engine
from app.routers import auth_routes

# Creates tables if they don't exist yet (fine for dev; use Alembic migrations in production)
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Phone OTP Auth API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # replace with your frontend's actual origin(s) in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Replaces FastAPI's default {"detail": [...]} validation error body with a
    simpler shape the frontend can read directly, e.g.:
        {"success": false, "field": "identifier", "message": "..."}
    Only the first error is surfaced; if the request had multiple invalid
    fields, the rest are still available in `errors` for debugging.
    """
    def clean_msg(msg: str) -> str:
        # Pydantic prefixes messages raised from @field_validator with
        # "Value error, " — strip that so the API doesn't leak internal wording.
        prefix = "Value error, "
        return msg[len(prefix):] if msg.startswith(prefix) else msg

    def field_name(loc) -> str:
        # loc is usually ("body", "identifier") — drop "body"/"query"/etc. and
        # join the rest in case of nested fields.
        parts = [str(p) for p in loc if p not in ("body", "query", "path")]
        return ".".join(parts) if parts else ""

    errors = exc.errors()
    first = errors[0]

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "success": False,
            "field": field_name(first["loc"]),
            "message": clean_msg(first["msg"]),
            "errors": [
                {"field": field_name(e["loc"]), "message": clean_msg(e["msg"])}
                for e in errors
            ],
        },
    )


app.include_router(auth_routes.router)


@app.get("/")
def health_check():
    return {"status": "ok"}