import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { useToast } from "../ui/toast";
import type { PublicConfig } from "../types";

// Поля-строки конфига; секреты (token, app-password) — отдельно, не показываем значение.
type Form = Record<string, string>;

const EMPTY: Form = {
  guild_id: "", discord_token: "",
  nextcloud_url: "", nextcloud_user: "", nextcloud_app_password: "", nextcloud_dir: "",
  author_ids: "", character_names: "", name_blacklist: "",
  text_contains: "", text_fuzzy: "", text_masks: "", fuzzy_threshold: "0.82",
  text_name_patterns: "", text_command_prefixes: "", text_ooc_prefixes: "",
  timezone: "", time_format: "",
};

export default function SettingsPage() {
  const toast = useToast();
  const [form, setForm] = useState<Form>(EMPTY);
  const [bools, setBools] = useState({ text_fallback_nick: false, text_ignore_bots: true });
  const [saving, setSaving] = useState(false);

  const { data: cfg } = useQuery({ queryKey: ["config"], queryFn: api.getConfig });

  useEffect(() => {
    if (!cfg) return;
    const next = { ...EMPTY };
    for (const k of Object.keys(EMPTY)) {
      const v = (cfg as PublicConfig)[k];
      if (typeof v === "string") next[k] = v;
    }
    setForm(next);
    setBools({
      text_fallback_nick: String(cfg.text_fallback_nick) === "true",
      text_ignore_bots: String(cfg.text_ignore_bots) !== "false",
    });
  }, [cfg]);

  const set = (k: string) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  async function save() {
    setSaving(true);
    try {
      await api.saveConfig({
        ...form,
        text_fallback_nick: String(bools.text_fallback_nick),
        text_ignore_bots: String(bools.text_ignore_bots),
      });
      toast("Настройки сохранены", "ok");
    } catch (e) {
      toast((e as Error).message, "err");
    } finally {
      setSaving(false);
    }
  }

  const Badge = ({ set }: { set?: boolean }) =>
    <span className={`badge ${set ? "ok" : "warn"}`}>{set ? "задано" : "не задано"}</span>;

  return (
    <div className="stack">
      <div className="row">
        <h1>Настройки</h1>
        <button className="btn primary right" onClick={save} disabled={saving}>
          {saving ? <span className="spinner" /> : "Сохранить"}
        </button>
      </div>

      <fieldset>
        <legend>Discord</legend>
        <div className="fields">
          <label className="field">ID сервера (Guild ID)
            <input className="input" value={form.guild_id} onChange={set("guild_id")} placeholder="123456789012345678" />
          </label>
          <label className="field">Токен бота <Badge set={cfg?.discord_token_set} />
            <input className="input" type="password" value={form.discord_token} onChange={set("discord_token")} placeholder="пусто — не менять" />
          </label>
        </div>
      </fieldset>

      <fieldset>
        <legend>Nextcloud</legend>
        <div className="fields">
          <label className="field">URL
            <input className="input" value={form.nextcloud_url} onChange={set("nextcloud_url")} placeholder="https://notes.omc.root.sx" />
          </label>
          <label className="field">Логин
            <input className="input" value={form.nextcloud_user} onChange={set("nextcloud_user")} placeholder="dave" />
          </label>
          <label className="field">App-password <Badge set={cfg?.nextcloud_app_password_set} />
            <input className="input" type="password" value={form.nextcloud_app_password} onChange={set("nextcloud_app_password")} placeholder="пусто — не менять" />
          </label>
          <label className="field">Папка по умолчанию
            <input className="input" value={form.nextcloud_dir} onChange={set("nextcloud_dir")} placeholder="discord-scrapes" />
          </label>
        </div>
      </fieldset>

      <fieldset>
        <legend>Кто такой персонаж</legend>
        <label className="field">ID авторов <span className="hint">через запятую · пусто = любые</span>
          <input className="input" value={form.author_ids} onChange={set("author_ids")} placeholder="напр. ID бота-персонажа" />
        </label>
        <div className="fields" style={{ marginTop: 12 }}>
          <label className="field">✅ Белый список имён <span className="hint">маски * ?</span>
            <textarea className="input" value={form.character_names} onChange={set("character_names")} placeholder="пусто = любой&#10;Джон*" />
          </label>
          <label className="field">⛔ Чёрный список имён <span className="hint">маски * ?</span>
            <textarea className="input" value={form.name_blacklist} onChange={set("name_blacklist")} placeholder="Система&#10;GM*" />
          </label>
        </div>
      </fieldset>

      <fieldset>
        <legend>Служебный текст — что выкидывать</legend>
        <label className="field">🚫 Содержит <span className="hint">подстрока, регистр не важен</span>
          <textarea className="input" value={form.text_contains} onChange={set("text_contains")} placeholder="вошёл в игру&#10;бросает кубик" />
        </label>
        <label className="field" style={{ marginTop: 12 }}>≈ Похоже на <span className="hint">примеры служебных сообщений</span>
          <textarea className="input" value={form.text_fuzzy} onChange={set("text_fuzzy")} placeholder="Игрок Имя вошёл в игру" />
        </label>
        <label className="field" style={{ marginTop: 12 }}>
          Порог похожести: <b>{form.fuzzy_threshold}</b> <span className="hint">выше = строже</span>
          <input type="range" min="0.5" max="1" step="0.01" value={form.fuzzy_threshold} onChange={set("fuzzy_threshold")} />
        </label>
        <details style={{ marginTop: 8 }}>
          <summary className="muted small">Продвинутое: маски по тексту (* ?)</summary>
          <textarea className="input" style={{ marginTop: 8 }} value={form.text_masks} onChange={set("text_masks")} placeholder="*вошёл в игру*" />
        </details>
      </fieldset>

      <fieldset>
        <legend>Сбор из текста</legend>
        <label className="field">Шаблоны имени <span className="hint">{"{name}"} — имя, {"{text}"} — реплика; по одному на строку</span>
          <textarea className="input" value={form.text_name_patterns} onChange={set("text_name_patterns")} placeholder="({name}) {text}" />
        </label>
        <label className="switch" style={{ margin: "10px 0" }}>
          <input type="checkbox" checked={bools.text_fallback_nick} onChange={(e) => setBools((b) => ({ ...b, text_fallback_nick: e.target.checked }))} />
          <span>Если шаблон не совпал — брать имя автора (fallback на ник)</span>
        </label>
        <label className="switch" style={{ margin: "10px 0" }}>
          <input type="checkbox" checked={bools.text_ignore_bots} onChange={(e) => setBools((b) => ({ ...b, text_ignore_bots: e.target.checked }))} />
          <span>Пропускать сообщения ботов</span>
        </label>
        <div className="fields">
          <label className="field">Префиксы команд <span className="hint">через пробел</span>
            <input className="input" value={form.text_command_prefixes} onChange={set("text_command_prefixes")} placeholder="! . /" />
          </label>
          <label className="field">Префиксы OOC <span className="hint">через пробел</span>
            <input className="input" value={form.text_ooc_prefixes} onChange={set("text_ooc_prefixes")} placeholder="(( // [" />
          </label>
        </div>
      </fieldset>

      <fieldset>
        <legend>Формат времени</legend>
        <div className="fields">
          <label className="field">Часовой пояс
            <input className="input" value={form.timezone} onChange={set("timezone")} placeholder="Europe/Warsaw" />
          </label>
          <label className="field">Формат метки
            <input className="input" value={form.time_format} onChange={set("time_format")} placeholder="%Y-%m-%d %H:%M:%S" />
          </label>
        </div>
      </fieldset>
    </div>
  );
}
