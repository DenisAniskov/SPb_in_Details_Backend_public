from typing import Optional
import math
from sqlalchemy import String, Float, Boolean, Text, CheckConstraint, Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base
from app.models.enums import PlaceCategory

# Visitor-approach statuses (Phase C3). A routing coordinate is exposed only
# with a verified status and a recorded source; anything else honestly reads
# as the representative display point.
VERIFIED_ROUTING_STATUSES = {"verified_public_entrance", "verified_public_approach"}


class Place(Base):
    __tablename__ = "places"
    __table_args__ = (CheckConstraint('recommended_visit_minutes BETWEEN 5 AND 180'),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, index=True)
    # Immutable starter-catalog identity; unrelated user rows remain unkeyed.
    catalog_key: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    short_description: Mapped[str] = mapped_column(String(500), nullable=False)
    full_description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[PlaceCategory] = mapped_column(SQLEnum(PlaceCategory), nullable=False, index=True)
    is_unusual: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    address: Mapped[str] = mapped_column(String(300), nullable=False)
    
    photo_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    working_hours: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)  # Comma separated
    rating: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    
    architectural_style: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    year_built: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    recommended_visit_minutes: Mapped[int] = mapped_column(default=30, server_default='30', nullable=False)

    @property
    def source_metadata(self):
        from app.db.seed_data import seed_metadata_for
        return seed_metadata_for(self.catalog_key)

    @property
    def source_url(self):
        return self.source_metadata.get("source_url")

    @property
    def coordinate_kind(self):
        from app.db.seed_data import same_coordinates
        metadata = self.source_metadata
        if not metadata or not same_coordinates(
            self.latitude, self.longitude, metadata['latitude'], metadata['longitude']
        ):
            return "unverified"
        return metadata.get("coordinate_kind", "unverified")

    @property
    def address_status(self):
        metadata = self.source_metadata
        if metadata.get("address") != self.address:
            return "unverified"
        return metadata.get("address_status", "unverified")

    # Editorial tourist content lives in the reviewed seed catalogue
    # (backend/app/db/seed_places.json), not in user-editable DB rows.
    @property
    def why_visit(self):
        return self.source_metadata.get("why_visit")

    @property
    def visit_note(self):
        return self.source_metadata.get("visit_note")

    # Copied from OSM tags (docs/osm_source_snapshot.json), not invented.
    @property
    def official_website(self):
        return self.source_metadata.get("official_website")

    @property
    def wikipedia_url(self):
        return self.source_metadata.get("wikipedia_url")

    # Phase C3: the display point stays the published representative point;
    # pedestrian destinations may use a verified entrance when one exists.
    @property
    def display_latitude(self):
        return self.latitude

    @property
    def display_longitude(self):
        return self.longitude

    def _verified_routing_point(self):
        metadata = self.source_metadata
        status = metadata.get("routing_access_status")
        latitude = metadata.get("routing_latitude")
        longitude = metadata.get("routing_longitude")
        source = metadata.get("routing_point_source")
        if (status not in VERIFIED_ROUTING_STATUSES
                or type(latitude) not in (int, float) or not math.isfinite(latitude)
                or type(longitude) not in (int, float) or not math.isfinite(longitude)
                or not isinstance(source, str) or not source.startswith("https://www.openstreetmap.org/")
                or not isinstance(metadata.get("routing_point_verified_on"), str)):
            return None
        return latitude, longitude, source, status

    @property
    def routing_latitude(self):
        point = self._verified_routing_point()
        return point[0] if point else None

    @property
    def routing_longitude(self):
        point = self._verified_routing_point()
        return point[1] if point else None

    @property
    def routing_point_source(self):
        point = self._verified_routing_point()
        return point[2] if point else None

    @property
    def routing_point_verified_on(self):
        point = self._verified_routing_point()
        if point:
            return self.source_metadata.get("routing_point_verified_on")
        return None

    @property
    def routing_access_status(self):
        point = self._verified_routing_point()
        if point:
            return point[3]
        metadata = self.source_metadata
        status = metadata.get("routing_access_status")
        if status in {"representative_point_only", "access_uncertain", "restricted_access"}:
            return status
        return "representative_point_only"

    def __repr__(self) -> str:
        return f"<Place id={self.id} title={self.title} category={self.category.value}>"
