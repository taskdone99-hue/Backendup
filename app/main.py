from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

app.include_router(auth_routes.router)


@app.get("/")
def health_check():
    return {"status": "ok"}
