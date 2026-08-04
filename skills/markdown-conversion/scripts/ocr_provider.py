"""Optional, page-local OCR providers for the native PDF adapter.

The provider boundary deliberately knows nothing about the canonical document
model.  It rasterizes one PDFium page, runs OCR, and returns text spans in PDF
canvas coordinates (``left, bottom, right, top``).  Layout classification,
native/OCR de-duplication, and quality warnings remain the adapter's job.
"""
from __future__ import annotations

import importlib.metadata
import math
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, runtime_checkable


Point = tuple[float, float]
BBox = tuple[float, float, float, float]
EngineFactory = Callable[[], Any]


class OcrProviderError(RuntimeError):
    """A configured OCR provider could not safely produce a result."""


class OcrUnavailableError(OcrProviderError):
    """The requested OCR backend or one of its runtime dependencies is absent."""


@dataclass(frozen=True, slots=True)
class OcrSettings:
    """Engine-independent OCR settings used by the PDF pipeline.

    ``mode`` is consumed by the PDF adapter (``off``, ``auto``, or ``force``),
    but it lives here so one validated settings object can be passed through the
    pipeline.  RapidOCR's ``ch`` recognition model covers Chinese and English.
    """

    mode: str = "auto"
    engine: str = "rapidocr"
    language: str = "ch"
    dpi: float = 300.0
    max_long_edge: int = 4096
    min_confidence: float = 0.5

    def __post_init__(self) -> None:
        normalized_mode = str(self.mode).strip().lower()
        normalized_engine = str(self.engine).strip().lower()
        normalized_language = str(self.language).strip()
        if normalized_mode not in {"off", "auto", "force"}:
            raise ValueError("OCR mode must be one of: off, auto, force")
        if not normalized_engine:
            raise ValueError("OCR engine must not be empty")
        if not normalized_language:
            raise ValueError("OCR language must not be empty")
        try:
            dpi = float(self.dpi)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("OCR dpi must be a number") from exc
        if not math.isfinite(dpi) or not 1.0 <= dpi <= 1200.0:
            raise ValueError("OCR dpi must be between 1 and 1200")
        try:
            max_long_edge = int(self.max_long_edge)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("OCR max_long_edge must be an integer") from exc
        if isinstance(self.max_long_edge, bool) or max_long_edge != self.max_long_edge:
            raise ValueError("OCR max_long_edge must be an integer")
        if not 1 <= max_long_edge <= 65535:
            raise ValueError("OCR max_long_edge must be between 1 and 65535")
        try:
            confidence = float(self.min_confidence)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("OCR min_confidence must be a number") from exc
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("OCR min_confidence must be between 0 and 1")
        object.__setattr__(self, "mode", normalized_mode)
        object.__setattr__(self, "engine", normalized_engine)
        object.__setattr__(self, "language", normalized_language)
        object.__setattr__(self, "dpi", dpi)
        object.__setattr__(self, "max_long_edge", max_long_edge)
        object.__setattr__(self, "min_confidence", confidence)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "OcrSettings":
        """Create settings from a config mapping and reject unknown keys."""

        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("OCR settings must be an object")
        allowed = {
            "mode", "engine", "language", "dpi", "max_long_edge", "min_confidence"
        }
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            raise ValueError(f"Unknown OCR setting(s): {', '.join(unknown)}")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class OcrSpan:
    """One OCR text line expressed in PDF canvas coordinates."""

    text: str
    confidence: float
    polygon: tuple[Point, Point, Point, Point]
    bbox: BBox


@dataclass(frozen=True, slots=True)
class OcrPageResult:
    """Normalized result of running one provider on one PDF page."""

    page_number: int
    engine: str
    engine_version: str
    runtime: str
    runtime_version: str
    model_profile: str
    language: str
    min_confidence: float
    spans: tuple[OcrSpan, ...]
    requested_dpi: float
    effective_dpi: float
    raster_width: int
    raster_height: int
    dropped_low_confidence: int = 0
    dropped_invalid: int = 0
    elapsed_seconds: float | None = None

    @property
    def text(self) -> str:
        return "\n".join(span.text for span in self.spans)


@runtime_checkable
class OcrProvider(Protocol):
    """Interface consumed by the PDF adapter."""

    name: str
    settings: OcrSettings

    @property
    def version(self) -> str: ...

    @property
    def available(self) -> bool: ...

    def extract(self, page: Any, page_number: int) -> OcrPageResult: ...


class NullOcrProvider:
    """No-op provider used when OCR is disabled."""

    name = "none"

    def __init__(self, settings: OcrSettings | None = None):
        self.settings = settings or OcrSettings(mode="off", engine="none")

    @property
    def version(self) -> str:
        return "not-applicable"

    @property
    def available(self) -> bool:
        return False

    def extract(self, _page: Any, page_number: int) -> OcrPageResult:
        return OcrPageResult(
            page_number=page_number,
            engine=self.name,
            engine_version=self.version,
            runtime="none",
            runtime_version="not-applicable",
            model_profile="none",
            language=self.settings.language,
            min_confidence=self.settings.min_confidence,
            spans=(),
            requested_dpi=self.settings.dpi,
            effective_dpi=0.0,
            raster_width=0,
            raster_height=0,
        )


class RapidOcrProvider:
    """Lazy RapidOCR 3.x provider backed by one reusable engine instance.

    Importing this module never imports RapidOCR.  The package, model runtime,
    and models are loaded only when :meth:`extract` is first called.  The same
    engine is then reused for all pages handled by this provider.  ``engine_factory``
    is intentionally injectable so tests do not need RapidOCR or model files.
    """

    name = "rapidocr"

    def __init__(
        self,
        settings: OcrSettings | None = None,
        *,
        engine_factory: EngineFactory | None = None,
    ):
        self.settings = settings or OcrSettings()
        if self.settings.engine not in {"rapidocr", "auto"}:
            raise ValueError(
                f"RapidOcrProvider cannot serve OCR engine {self.settings.engine!r}"
            )
        self._engine_factory = engine_factory
        self._engine: Any | None = None
        self._engine_failure: tuple[type[OcrProviderError], str] | None = None
        # RapidOCR mutates per-call options internally.  One re-entrant lock
        # protects both singleton initialization and inference on the shared
        # instance if a future caller parallelizes page conversion.
        self._engine_lock = threading.RLock()

    @property
    def version(self) -> str:
        try:
            return importlib.metadata.version("rapidocr")
        except importlib.metadata.PackageNotFoundError:
            return "injected" if self._engine_factory is not None else "not-installed"
        except Exception:
            return "unknown"

    @property
    def available(self) -> bool:
        if self._engine_failure is not None:
            return False
        if self._engine is not None or self._engine_factory is not None:
            return True
        try:
            importlib.metadata.version("rapidocr")
            return True
        except importlib.metadata.PackageNotFoundError:
            return False
        except Exception:
            return False

    def _default_engine_factory(self) -> Any:
        try:
            from rapidocr import LangRec, RapidOCR
        except (ImportError, ModuleNotFoundError) as exc:
            raise OcrUnavailableError(
                "RapidOCR is not installed or incompatible; install the tested "
                "'rapidocr==3.9.2' and 'onnxruntime>=1.20,<2'"
            ) from exc

        try:
            language: Any
            try:
                language = LangRec(self.settings.language)
            except ValueError:
                # RapidOCR also accepts custom language/model identifiers.
                language = self.settings.language
            return RapidOCR(
                params={
                    # Filtering is performed here so all backends have the same
                    # threshold semantics and dropped detections can be counted.
                    "Global.text_score": 0.0,
                    "Global.log_level": "critical",
                    "Rec.lang_type": language,
                }
            )
        except OcrProviderError:
            raise
        except (ImportError, ModuleNotFoundError) as exc:
            raise OcrUnavailableError(
                "RapidOCR is installed but its configured inference runtime is unavailable"
            ) from exc
        except Exception as exc:
            raise OcrProviderError(
                f"RapidOCR engine initialization failed ({type(exc).__name__})"
            ) from exc

    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        if self._engine_failure is not None:
            error_type, message = self._engine_failure
            raise error_type(message)
        with self._engine_lock:
            if self._engine is not None:
                return self._engine
            if self._engine_failure is not None:
                error_type, message = self._engine_failure
                raise error_type(message)
            factory = self._engine_factory or self._default_engine_factory
            try:
                engine = factory()
            except OcrProviderError as exc:
                self._engine_failure = (type(exc), str(exc))
                raise
            except (ImportError, ModuleNotFoundError) as exc:
                error = OcrUnavailableError(
                    "The configured RapidOCR engine is unavailable"
                )
                self._engine_failure = (type(error), str(error))
                raise error from exc
            except Exception as exc:
                error = OcrProviderError(
                    f"RapidOCR engine initialization failed ({type(exc).__name__})"
                )
                self._engine_failure = (type(error), str(error))
                raise error from exc
            if engine is None or not callable(engine):
                error = OcrProviderError(
                    "RapidOCR engine factory returned a non-callable object"
                )
                self._engine_failure = (type(error), str(error))
                raise error
            self._engine = engine
            return engine

    def _runtime_metadata(self) -> tuple[str, str]:
        if self._engine_factory is not None:
            return "injected", "injected"
        try:
            return "onnxruntime", importlib.metadata.version("onnxruntime")
        except importlib.metadata.PackageNotFoundError:
            return "onnxruntime", "not-installed"
        except Exception:
            return "onnxruntime", "unknown"

    def _render_scale(self, page: Any) -> tuple[float, float, float]:
        try:
            width, height = page.get_size()
            width, height = float(width), float(height)
        except Exception as exc:
            raise OcrProviderError(
                f"Could not read PDF page dimensions ({type(exc).__name__})"
            ) from exc
        if (
            not math.isfinite(width)
            or not math.isfinite(height)
            or width <= 0.0
            or height <= 0.0
        ):
            raise OcrProviderError("PDF page dimensions are invalid for OCR")
        requested_scale = self.settings.dpi / 72.0
        capped_scale = self.settings.max_long_edge / max(width, height)
        scale = min(requested_scale, capped_scale)
        if capped_scale < requested_scale:
            # PdfPage.render() rounds dimensions up with ceil().  Moving one
            # representable float below the exact cap prevents a precision
            # artifact from producing max_long_edge + 1 pixels.
            scale = math.nextafter(scale, 0.0)
        if not math.isfinite(scale) or scale <= 0.0:
            raise OcrProviderError("Calculated OCR raster scale is invalid")
        return width, height, scale

    @staticmethod
    def _as_sequence(value: Any, field: str) -> list[Any]:
        if value is None:
            return []
        try:
            return list(value)
        except (TypeError, ValueError) as exc:
            raise OcrProviderError(f"RapidOCR returned an invalid {field} collection") from exc

    @staticmethod
    def _pixel_polygon(value: Any) -> tuple[Point, Point, Point, Point] | None:
        try:
            points = list(value)
        except (TypeError, ValueError):
            return None
        if len(points) != 4:
            return None
        normalized: list[Point] = []
        for point in points:
            try:
                coordinates = list(point)
                if len(coordinates) != 2:
                    return None
                x, y = float(coordinates[0]), float(coordinates[1])
            except (TypeError, ValueError, OverflowError):
                return None
            if not math.isfinite(x) or not math.isfinite(y):
                return None
            normalized.append((x, y))
        return (normalized[0], normalized[1], normalized[2], normalized[3])

    @staticmethod
    def _to_page_polygon(
        pixel_polygon: tuple[Point, Point, Point, Point], posconv: Any
    ) -> tuple[Point, Point, Point, Point]:
        mapped: list[Point] = []
        for x, y in pixel_polygon:
            try:
                page_x, page_y = posconv.to_page(int(round(x)), int(round(y)))
                page_x, page_y = float(page_x), float(page_y)
            except Exception as exc:
                raise OcrProviderError(
                    f"Could not map OCR coordinates to the PDF page ({type(exc).__name__})"
                ) from exc
            if not math.isfinite(page_x) or not math.isfinite(page_y):
                raise OcrProviderError("OCR coordinate mapping produced a non-finite value")
            mapped.append((round(page_x, 3), round(page_y, 3)))
        return (mapped[0], mapped[1], mapped[2], mapped[3])

    @staticmethod
    def _bbox(polygon: tuple[Point, Point, Point, Point]) -> BBox:
        xs = [point[0] for point in polygon]
        ys = [point[1] for point in polygon]
        return (
            round(min(xs), 3),
            round(min(ys), 3),
            round(max(xs), 3),
            round(max(ys), 3),
        )

    def _normalize_output(
        self,
        output: Any,
        posconv: Any,
    ) -> tuple[tuple[OcrSpan, ...], int, int, float | None]:
        if output is None:
            raise OcrProviderError("RapidOCR returned no result object")
        boxes_value = getattr(output, "boxes", None)
        texts_value = getattr(output, "txts", None)
        scores_value = getattr(output, "scores", None)
        if boxes_value is None and texts_value is None and scores_value is None:
            return (), 0, 0, self._elapsed(output)
        if boxes_value is None or texts_value is None or scores_value is None:
            raise OcrProviderError("RapidOCR returned an incomplete result object")

        boxes = self._as_sequence(boxes_value, "boxes")
        texts = self._as_sequence(texts_value, "txts")
        scores = self._as_sequence(scores_value, "scores")
        if not (len(boxes) == len(texts) == len(scores)):
            raise OcrProviderError(
                "RapidOCR returned boxes, texts, and scores with different lengths"
            )

        spans: list[OcrSpan] = []
        dropped_low_confidence = 0
        dropped_invalid = 0
        for box, text_value, score_value in zip(boxes, texts, scores, strict=True):
            text = str(text_value).strip() if text_value is not None else ""
            try:
                confidence = float(score_value)
            except (TypeError, ValueError, OverflowError):
                confidence = math.nan
            pixel_polygon = self._pixel_polygon(box)
            if (
                not text
                or not math.isfinite(confidence)
                or not 0.0 <= confidence <= 1.0
                or pixel_polygon is None
            ):
                dropped_invalid += 1
                continue
            if confidence < self.settings.min_confidence:
                dropped_low_confidence += 1
                continue
            polygon = self._to_page_polygon(pixel_polygon, posconv)
            spans.append(
                OcrSpan(
                    text=text,
                    confidence=round(confidence, 6),
                    polygon=polygon,
                    bbox=self._bbox(polygon),
                )
            )
        return (
            tuple(spans),
            dropped_low_confidence,
            dropped_invalid,
            self._elapsed(output),
        )

    @staticmethod
    def _elapsed(output: Any) -> float | None:
        try:
            value = getattr(output, "elapse", None)
            if value is None:
                return None
            elapsed = float(value)
            return elapsed if math.isfinite(elapsed) and elapsed >= 0.0 else None
        except (TypeError, ValueError, OverflowError):
            return None

    def extract(self, page: Any, page_number: int) -> OcrPageResult:
        """Rasterize and OCR one PDFium page.

        Returned polygons and bounding boxes use PDF canvas coordinates.  The
        PDFium bitmap position converter is used instead of manual y-axis
        inversion, which keeps crop boxes and rotated pages correct.
        """

        try:
            normalized_page_number = int(page_number)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("page_number must be a positive integer") from exc
        if (
            isinstance(page_number, bool)
            or normalized_page_number != page_number
            or normalized_page_number < 1
        ):
            raise ValueError("page_number must be a positive integer")
        page_number = normalized_page_number
        _, _, scale = self._render_scale(page)
        engine = self._get_engine()
        try:
            bitmap = page.render(
                scale=scale,
                fill_color=(255, 255, 255, 255),
                maybe_alpha=False,
            )
        except Exception as exc:
            raise OcrProviderError(
                f"Could not rasterize PDF page for OCR ({type(exc).__name__})"
            ) from exc

        source_image: Any | None = None
        image: Any | None = None
        try:
            try:
                posconv = bitmap.get_posconv(page)
                source_image = bitmap.to_pil()
                image = source_image.convert("RGB")
                raster_width, raster_height = image.size
            except Exception as exc:
                raise OcrProviderError(
                    f"Could not prepare the OCR page image ({type(exc).__name__})"
                ) from exc

            try:
                # RapidOCR 3.x accepts PIL images and returns RapidOCROutput,
                # whose boxes/txts/scores fields are normalized below.
                with self._engine_lock:
                    output = engine(image, return_word_box=False)
            except OcrProviderError:
                raise
            except (ImportError, ModuleNotFoundError) as exc:
                raise OcrUnavailableError(
                    "RapidOCR's inference runtime is unavailable; install "
                    "'onnxruntime>=1.20,<2'"
                ) from exc
            except Exception as exc:
                raise OcrProviderError(
                    f"RapidOCR inference failed ({type(exc).__name__})"
                ) from exc
            spans, dropped_low, dropped_invalid, elapsed = self._normalize_output(
                output, posconv
            )
            runtime, runtime_version = self._runtime_metadata()
            return OcrPageResult(
                page_number=page_number,
                engine=self.name,
                engine_version=self.version,
                runtime=runtime,
                runtime_version=runtime_version,
                model_profile="PP-OCRv6-small",
                language=self.settings.language,
                min_confidence=self.settings.min_confidence,
                spans=spans,
                requested_dpi=self.settings.dpi,
                effective_dpi=round(scale * 72.0, 3),
                raster_width=int(raster_width),
                raster_height=int(raster_height),
                dropped_low_confidence=dropped_low,
                dropped_invalid=dropped_invalid,
                elapsed_seconds=elapsed,
            )
        finally:
            if image is not None:
                try:
                    image.close()
                except Exception:
                    pass
            if source_image is not None and source_image is not image:
                try:
                    source_image.close()
                except Exception:
                    pass
            try:
                bitmap.close()
            except Exception:
                pass


__all__ = [
    "BBox",
    "NullOcrProvider",
    "OcrPageResult",
    "OcrProvider",
    "OcrProviderError",
    "OcrSettings",
    "OcrSpan",
    "OcrUnavailableError",
    "Point",
    "RapidOcrProvider",
]
