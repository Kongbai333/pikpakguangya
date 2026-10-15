<script setup>
/** 管理后台登录页。 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowRight, KeyRound, LockKeyhole, ShieldCheck } from 'lucide-vue-next'

import { loginWeb, webAuth } from '../auth'

const route = useRoute()
const router = useRouter()
const password = ref('')
const loading = ref(false)
const error = ref('')
const shake = ref(0)
const inputEl = ref(null)

const ready = computed(() => !!password.value && !loading.value)

async function submit() {
  if (!password.value) {
    error.value = '请输入管理员密码'
    bump()
    return
  }
  loading.value = true
  error.value = ''
  try {
    await loginWeb(password.value)
    const target = typeof route.query.redirect === 'string' ? route.query.redirect : '/admin'
    await router.replace(target.startsWith('/') ? target : '/admin')
  } catch (e) {
    error.value = e.message
    bump()
  } finally {
    loading.value = false
  }
}

/** 报错时抖一下：比只冒一行红字更容易被注意到 */
function bump() {
  shake.value += 1
}

onMounted(() => inputEl.value?.focus())
</script>

<template>
  <div class="login">
    <!-- 背景：三团缓慢漂移的光斑，页面「活着」但不会抢注意力 -->
    <div class="aurora" aria-hidden="true">
      <span class="aurora__blob aurora__blob--a" />
      <span class="aurora__blob aurora__blob--b" />
      <span class="aurora__blob aurora__blob--c" />
      <span class="aurora__grid" />
    </div>

    <main class="panel">
      <!-- 左：品牌 -->
      <aside class="brand">
        <div class="brand__top">
          <span class="brand-mark">PK</span>
          <div>
            <div class="brand__name">PikPak 链接库</div>
            <div class="brand__sub">采集 · 校验 · 秒传</div>
          </div>
        </div>

        <div class="brand__pitch">
          <h2>把散在群里的分享<br />收进一个地方</h2>
          <ul class="brand__list">
            <li><ShieldCheck :size="15" /> 数据全部存在本机，不上传任何地方</li>
            <li><KeyRound :size="15" /> 密码只保存在 config.toml，不做任何远程校验</li>
          </ul>
        </div>

        <div class="brand__foot">Telegram 采集 · PikPak 校验 · 光鸭秒传</div>
      </aside>

      <!-- 右：表单 -->
      <section class="form">
        <div class="form__head">
          <h1>管理员登录</h1>
          <p>输入本机配置的管理员密码后继续</p>
        </div>

        <label class="field">
          <span class="field__label">密码</span>
          <n-input
            ref="inputEl"
            v-model:value="password"
            size="large"
            type="password"
            show-password-on="click"
            placeholder="管理员密码"
            :disabled="loading"
            :input-props="{ autocomplete: 'current-password' }"
            @keyup.enter="submit"
          >
            <template #prefix><LockKeyhole :size="16" /></template>
          </n-input>
        </label>

        <button
          class="submit"
          type="button"
          :class="{ 'is-ready': ready }"
          :disabled="loading"
          @click="submit"
        >
          <span v-if="loading" class="submit__spin" aria-hidden="true" />
          <span class="submit__text">{{ loading ? '正在登录…' : '登录' }}</span>
          <ArrowRight v-if="!loading" :size="17" class="submit__arrow" />
        </button>

        <Transition name="slide-fade">
          <div :key="shake" class="notice" :class="{ shake: shake > 0 }" v-if="error || !webAuth.configured">
            <template v-if="error">{{ error }}</template>
            <template v-else>
              尚未配置管理员密码，请设置 <code class="mono">config.toml</code> 的
              <code class="mono">[admin] password</code>。
            </template>
          </div>
        </Transition>

        <div class="form__foot">
          <RouterLink to="/" class="form__link">先去前台浏览</RouterLink>
          <span class="form__dot">·</span>
          <span>忘记密码？改 <code class="mono">config.toml</code> 后重启服务</span>
        </div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.login {
  position: relative;
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 28px 20px;
  overflow: hidden;
  background: var(--app-bg);
}

/* ==========================================================================
   背景光斑
   ========================================================================== */
.aurora {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

.aurora__blob {
  position: absolute;
  border-radius: 50%;
  filter: blur(70px);
  opacity: 0.5;
  will-change: transform;
}

.aurora__blob--a {
  width: 46vw;
  height: 46vw;
  min-width: 320px;
  min-height: 320px;
  top: -14vw;
  left: -8vw;
  background: color-mix(in srgb, var(--app-brand) 34%, transparent);
  animation: drift-a 22s ease-in-out infinite;
}

.aurora__blob--b {
  width: 38vw;
  height: 38vw;
  min-width: 260px;
  min-height: 260px;
  bottom: -12vw;
  right: -6vw;
  background: color-mix(in srgb, var(--app-ok) 26%, transparent);
  animation: drift-b 27s ease-in-out infinite;
}

.aurora__blob--c {
  width: 26vw;
  height: 26vw;
  min-width: 200px;
  min-height: 200px;
  top: 42%;
  left: 46%;
  background: color-mix(in srgb, var(--app-warn) 18%, transparent);
  animation: drift-c 33s ease-in-out infinite;
}

/* 一层很淡的网格，给纯色背景一点材质 */
.aurora__grid {
  position: absolute;
  inset: 0;
  opacity: 0.5;
  background-image:
    linear-gradient(color-mix(in srgb, var(--app-line) 60%, transparent) 1px, transparent 1px),
    linear-gradient(90deg, color-mix(in srgb, var(--app-line) 60%, transparent) 1px, transparent 1px);
  background-size: 46px 46px;
  mask-image: radial-gradient(ellipse 70% 60% at 50% 45%, #000 30%, transparent 100%);
}

/* ==========================================================================
   主面板
   ========================================================================== */
.panel {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: minmax(0, 0.92fr) minmax(0, 1fr);
  width: min(920px, 100%);
  overflow: hidden;
  border: 1px solid var(--app-line);
  border-radius: 24px;
  background: color-mix(in srgb, var(--app-bg) 88%, transparent);
  backdrop-filter: blur(20px) saturate(150%);
  box-shadow: var(--shadow-3);
  animation: panel-in 0.55s var(--ease-out) both;
}

@keyframes panel-in {
  from {
    opacity: 0;
    transform: translateY(16px) scale(0.985);
  }
}

/* ---------- 左：品牌 ---------- */
.brand {
  display: flex;
  flex-direction: column;
  gap: 28px;
  padding: 34px 32px;
  background:
    radial-gradient(
      130% 100% at 0% 0%,
      color-mix(in srgb, var(--app-brand) 12%, transparent),
      transparent 62%
    ),
    var(--app-panel);
  border-right: 1px solid var(--app-line);
}

.brand__top {
  display: flex;
  align-items: center;
  gap: 11px;
}

.brand__name {
  color: var(--app-text);
  font-size: 14.5px;
  font-weight: 700;
  line-height: 1.2;
}

.brand__sub {
  margin-top: 2px;
  color: var(--app-faint);
  font-size: 12px;
}

.brand__pitch {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 20px;
}

.brand__pitch h2 {
  margin: 0;
  color: var(--app-text);
  font-size: 24px;
  font-weight: 700;
  line-height: 1.4;
  letter-spacing: -0.3px;
}

.brand__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.brand__list li {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  color: var(--app-muted);
  font-size: 12.5px;
  line-height: 1.55;
}

.brand__list svg {
  flex: 0 0 auto;
  margin-top: 1px;
  color: var(--app-brand);
}

.brand__foot {
  color: var(--app-faint);
  font-size: 11.5px;
  letter-spacing: 0.2px;
}

/* ---------- 右：表单 ---------- */
.form {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 18px;
  padding: 40px 38px;
}

.form__head h1 {
  margin: 0;
  color: var(--app-text);
  font-size: 26px;
  font-weight: 700;
  line-height: 1.2;
  letter-spacing: -0.4px;
}

.form__head p {
  margin: 8px 0 0;
  color: var(--app-muted);
  font-size: 13px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin-top: 6px;
}

.field__label {
  color: var(--app-muted);
  font-size: 12.5px;
}

/* 主按钮：可点的时候才亮起来，有渐变的呼吸感 */
.submit {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 9px;
  height: 46px;
  margin-top: 4px;
  padding: 0 20px;
  border: 0;
  border-radius: 13px;
  background: linear-gradient(
    135deg,
    var(--app-brand),
    color-mix(in srgb, var(--app-brand) 62%, #7c3aed)
  );
  background-size: 160% 160%;
  color: #fff;
  font-family: inherit;
  font-size: 14.5px;
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 8px 22px -10px color-mix(in srgb, var(--app-brand) 80%, transparent);
  transition:
    transform var(--dur-fast) ease,
    box-shadow var(--dur) var(--ease-out),
    background-position 0.6s var(--ease-out),
    opacity var(--dur-fast) ease;
}

.submit:hover:not(:disabled) {
  background-position: 100% 50%;
  transform: translateY(-1px);
  box-shadow: 0 12px 28px -10px color-mix(in srgb, var(--app-brand) 85%, transparent);
}

.submit:active:not(:disabled) {
  transform: translateY(0);
}

.submit:disabled {
  opacity: 0.62;
  cursor: default;
}

.submit__arrow {
  transition: transform var(--dur) var(--ease-spring);
}

.submit:hover:not(:disabled) .submit__arrow {
  transform: translateX(3px);
}

.submit__spin {
  width: 15px;
  height: 15px;
  border: 2px solid rgba(255, 255, 255, 0.35);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* ---------- 错误提示 ---------- */
.notice {
  padding: 11px 13px;
  border: 1px solid color-mix(in srgb, var(--app-danger) 30%, transparent);
  border-radius: 11px;
  background: color-mix(in srgb, var(--app-danger) 9%, transparent);
  color: var(--app-danger);
  font-size: 12.5px;
  line-height: 1.55;
}

.notice code {
  padding: 1px 4px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--app-danger) 12%, transparent);
  font-size: 12px;
}

.shake {
  animation: shake 0.42s cubic-bezier(0.36, 0.07, 0.19, 0.97);
}

@keyframes shake {
  10%, 90% { transform: translateX(-2px); }
  20%, 80% { transform: translateX(4px); }
  30%, 50%, 70% { transform: translateX(-6px); }
  40%, 60% { transform: translateX(6px); }
}

.slide-fade-enter-active {
  transition: opacity var(--dur) ease, transform var(--dur) var(--ease-out);
}

.slide-fade-enter-from {
  opacity: 0;
  transform: translateY(-6px);
}

.form__foot {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  color: var(--app-faint);
  font-size: 11.5px;
}

.form__link {
  color: var(--app-muted);
  text-decoration: none;
}

.form__link:hover {
  color: var(--app-brand);
  text-decoration: underline;
}

.form__dot {
  opacity: 0.5;
}

.form__foot code {
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--app-panel);
  font-size: 11px;
}

/* ==========================================================================
   背景动画
   ========================================================================== */
@keyframes drift-a {
  0%, 100% { transform: translate(0, 0) scale(1); }
  33% { transform: translate(6vw, 4vh) scale(1.12); }
  66% { transform: translate(-3vw, 7vh) scale(0.94); }
}

@keyframes drift-b {
  0%, 100% { transform: translate(0, 0) scale(1); }
  40% { transform: translate(-7vw, -5vh) scale(1.15); }
  70% { transform: translate(3vw, -2vh) scale(0.92); }
}

@keyframes drift-c {
  0%, 100% { transform: translate(-50%, -50%) scale(1); opacity: 0.4; }
  50% { transform: translate(-58%, -44%) scale(1.25); opacity: 0.6; }
}

/* ==========================================================================
   窄屏
   ========================================================================== */
@media (max-width: 760px) {
  .login {
    padding: 16px 12px;
  }

  .panel {
    grid-template-columns: minmax(0, 1fr);
    border-radius: 20px;
  }

  .brand {
    gap: 16px;
    padding: 22px 22px 18px;
    border-right: 0;
    border-bottom: 1px solid var(--app-line);
  }

  .brand__pitch h2 {
    font-size: 19px;
  }

  .brand__list,
  .brand__foot {
    display: none;
  }

  .form {
    padding: 26px 22px 30px;
  }

  .form__head h1 {
    font-size: 22px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .panel,
  .aurora__blob,
  .submit__spin {
    animation: none;
  }
  .shake {
    animation: none;
  }
}
</style>
