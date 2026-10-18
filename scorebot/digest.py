"""Shared embeds: the matchup preview line and the weekly digest."""
from __future__ import annotations

import discord

from .models import nflverse as nv
from .predictions import track_record


def matchup_line(p: dict) -> str:
    """'BUF 64% over KC · by 3.5 · 49 pts (KC 23 – 26 BUF)'."""
    fav, dog, pf = (p["home"], p["away"], p["p_home"]) if p["p_home"] >= 0.5 else (p["away"], p["home"], 1 - p["p_home"])
    hp, ap = round(p["home_pts"]), round(p["away_pts"])
    return (f"**{fav}** {pf:.0%} over {dog} · by {abs(p['margin']):.1f} · {p['total']:.0f} pts "
            f"({p['away']} {ap} – {hp} {p['home']})")


def chunk_lines(lines: list[str], limit: int = 1024) -> list[str]:
    """Pack whole lines into blocks that fit an embed field (never cut a line in half)."""
    out, cur = [], ""
    for line in lines:
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line[:limit]
    return out + ([cur] if cur else [])


async def build_digest(bot) -> discord.Embed:
    svc = bot.model
    preds = [p for g in svc.upcoming() if (p := svc.predict_game(g))]
    week = preds[0]["week"] if preds else None
    e = discord.Embed(title=f"🏈 ScoreBot weekly preview" + (f" · Week {week}" if week else ""), color=0x1ABC9C)
    for i, chunk in enumerate(chunk_lines([matchup_line(p) for p in preds])):
        e.add_field(name="Projections" if i == 0 else "\u200b", value=chunk, inline=False)
    top = svc.ratings()[:5]
    if top:
        e.add_field(name="Power top 5", inline=False,
                    value="\n".join(f"{i}. **{t['code']}** {t['power']:.0f}" for i, t in enumerate(top, 1)))
    rec = await track_record(bot.db, nv.current_season())
    if rec.get("games"):
        e.add_field(name="Season track record", inline=False,
                    value=f"{rec['correct']}/{rec['decided']} winners ({rec['accuracy']:.0%}) · "
                          f"avg margin miss {rec['margin_mae']:.1f} pts")
    e.set_footer(text="Elo + EPA + QB model · data: nflverse & ESPN")
    return e
