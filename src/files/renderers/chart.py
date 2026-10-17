"""Chart rendering to PNG with matplotlib's non-interactive backend."""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from src.core.errors import ValidationFailure
from src.files.specs import ChartSpec

_PALETTE = ["#38bdf8", "#f97316", "#a3e635", "#e879f9", "#facc15", "#f43f5e", "#2dd4bf", "#94a3b8"]


def render_chart(spec: ChartSpec) -> bytes:
    """Render ``spec`` as a PNG."""
    dpi = 100
    fig, ax = plt.subplots(figsize=(spec.width_px / dpi, spec.height_px / dpi), dpi=dpi)
    try:
        fig.patch.set_facecolor("white")
        if spec.type == "pie":
            series = spec.series[0]
            labels = spec.categories or [str(i + 1) for i in range(len(series.y))]
            if len(labels) != len(series.y):
                raise ValidationFailure("pie charts need one category per value")
            if any(v < 0 for v in series.y):
                raise ValidationFailure("pie charts cannot have negative values")
            ax.pie(series.y, labels=labels, autopct="%1.1f%%", colors=_PALETTE)
        elif spec.type == "histogram":
            for i, s in enumerate(spec.series):
                ax.hist(
                    s.y,
                    bins=spec.bins,
                    alpha=0.7,
                    label=s.name or None,
                    color=_PALETTE[i % len(_PALETTE)],
                )
        elif spec.type == "bar":
            n = len(spec.series)
            first = spec.series[0]
            labels = spec.categories or [str(x) for x in (first.x or range(1, len(first.y) + 1))]
            width = 0.8 / n
            for i, s in enumerate(spec.series):
                if len(s.y) != len(labels):
                    raise ValidationFailure("every bar series needs one value per category")
                bar_xs = [j + i * width for j in range(len(labels))]
                ax.bar(
                    bar_xs,
                    s.y,
                    width=width,
                    label=s.name or None,
                    color=_PALETTE[i % len(_PALETTE)],
                )
            ax.set_xticks([j + 0.4 - width / 2 for j in range(len(labels))])
            ax.set_xticklabels(
                labels,
                rotation=30 if len(labels) > 6 else 0,
                ha="right" if len(labels) > 6 else "center",
            )
        else:
            for i, s in enumerate(spec.series):
                xs: list[float | str] | list[int] = (
                    s.x if s.x is not None else list(range(1, len(s.y) + 1))
                )
                if len(xs) != len(s.y):
                    raise ValidationFailure("x and y must have the same length")
                color = _PALETTE[i % len(_PALETTE)]
                if spec.type == "scatter":
                    ax.scatter(xs, s.y, label=s.name or None, color=color)
                else:
                    ax.plot(xs, s.y, label=s.name or None, color=color, marker="o", markersize=3)
        if spec.type != "pie":
            ax.set_xlabel(spec.x_label)
            ax.set_ylabel(spec.y_label)
            ax.grid(alpha=0.25)
        if spec.title:
            ax.set_title(spec.title)
        if spec.legend and spec.type != "pie" and any(s.name for s in spec.series):
            ax.legend()
        fig.tight_layout()
        out = io.BytesIO()
        fig.savefig(out, format="png", metadata={"Software": ""})
        return out.getvalue()
    finally:
        plt.close(fig)
