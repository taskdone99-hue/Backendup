import enum

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base


class OTPChannel(str, enum.Enum):
    phone = "phone"
    email = "email"


class OTPPurpose(str, enum.Enum):
    signup = "signup"
    login = "login"
    reset_password = "reset_password"


class Gender(str, enum.Enum):
    male = "male"
    female = "female"
    non_binary = "non_binary"
    prefer_not_to_say = "prefer_not_to_say"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    # Chosen during signup, must be globally unique — checked ahead of time via
    # /check-username, same pattern as Instagram's signup flow.
    username = Column(String(30), unique=True, index=True, nullable=False)
    phone_number = Column(String(20), unique=True, index=True, nullable=True)
    email = Column(String(255), unique=True, index=True, nullable=True)
    # Nullable: users who only ever sign in via phone/email OTP have no password set.
    hashed_password = Column(String(255), nullable=True)
    date_of_birth = Column(Date, nullable=True)
    gender = Column(Enum(Gender), nullable=True)
    is_phone_verified = Column(Boolean, default=False, nullable=False)
    is_email_verified = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    refresh_tokens = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )


class PendingSignup(Base):
    """
    Holds a signup's username/password/DOB after /register but before the OTP
    step confirms it. The real User row is only created once /verify-otp
    succeeds for purpose=signup — this keeps unverified identifiers/usernames
    from permanently reserving a spot, matching Instagram's "you're not
    signed up until you enter the code" behavior.
    """

    __tablename__ = "pending_signups"

    id = Column(Integer, primary_key=True, index=True)
    # One pending signup per identifier; a repeat /register call overwrites it.
    identifier = Column(String(255), unique=True, index=True, nullable=False)
    channel = Column(Enum(OTPChannel), nullable=False)
    username = Column(String(30), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    date_of_birth = Column(Date, nullable=False)
    gender = Column(Enum(Gender), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class OTP(Base):
    __tablename__ = "otp_codes"

    id = Column(Integer, primary_key=True, index=True)
    # Phone number (E.164) or email address, depending on `channel`.
    identifier = Column(String(255), index=True, nullable=False)
    channel = Column(Enum(OTPChannel), nullable=False)
    purpose = Column(Enum(OTPPurpose), nullable=False)
    otp_hash = Column(String(255), nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    is_used = Column(Boolean, default=False, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Only a hash of the refresh token is stored — the raw value is returned to
    # the client once and never persisted, same pattern as the OTP hashing.
    token_hash = Column(String(255), unique=True, index=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="refresh_tokens")
