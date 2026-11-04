/*!
 * 前端 Markdown 渲染器回归测试（Node 直接运行，不需要任何依赖）
 *
 *   node tests/frontend/markdown.test.js
 *
 * 覆盖：块级结构（标题/表格/列表/引用/分割线/代码块）、行内语法、
 *      流式未闭合代码块，以及 XSS 转义等安全用例。
 */
"use strict";

const path = require("path");
const { renderMarkdown } = require(path.join(__dirname, "..", "..", "web", "markdown.js"));

const cases = [];
function check(name, fn) {
  cases.push({ name, fn });
}

/* -------------------------------------------------------------- 块级结构 */

check("一级/三级标题", (html) => {
  const out = renderMarkdown("# 标题一\n\n### 小节");
  return out.includes("<h1>标题一</h1>") && out.includes("<h3>小节</h3>");
});

check("段落与软换行", (html) => {
  const out = renderMarkdown("第一行\n第二行\n\n第二段");
  return out.includes("<p>第一行<br>第二行</p>") && out.includes("<p>第二段</p>");
});

check("GFM 表格含对齐与单元格行内代码", (html) => {
  const out = renderMarkdown(
    "| 工具 | 作用 | 权限 |\n| :--- | :---: | ---: |\n| `read_workspace_file` | 读取 | 否 |\n| `web_search` | 搜索 | 否 |"
  );
  return (
    out.includes('<div class="table-wrap"><table>') &&
    out.includes('<th style="text-align:center">作用</th>') &&
    out.includes('<td style="text-align:right">否</td>') &&
    out.includes("<code>read_workspace_file</code>")
  );
});

check("表格列数不齐也能渲染", (html) => {
  const out = renderMarkdown("| a | b |\n| --- | --- |\n| 1 |");
  return out.includes("<table>") && out.includes("<td>1</td>");
});

check("无序/有序列表与一级嵌套", (html) => {
  const out = renderMarkdown("1. 第一步\n2. 第二步\n   - 子项 A\n   - 子项 B\n3. 第三步");
  return (
    out.includes("<ol>") &&
    out.includes("<li>第一步</li>") &&
    /<li>第二步<ul><li>子项 A<\/li><li>子项 B<\/li><\/ul><\/li>/.test(out.replace(/\n/g, ""))
  );
});

check("任务清单", (html) => {
  const out = renderMarkdown("- [x] 已完成\n- [ ] 未完成");
  return out.includes("☑") && out.includes("☐") && out.includes("task done");
});

check("引用块多行合并", (html) => {
  const out = renderMarkdown("> 第一行\n> 第二行");
  return out.includes("<blockquote>第一行<br>第二行</blockquote>");
});

check("分割线", (html) => renderMarkdown("---").includes("<hr>"));

check("围栏代码块：语言标签 + HTML 转义 + 复制载荷", (html) => {
  const out = renderMarkdown("```python\nprint('<b>x</b>')\n```");
  return (
    out.includes('class="language-python"') &&
    out.includes("&lt;b&gt;") &&
    !out.includes("<b>x</b>") &&
    out.includes('data-copy="print(&#39;')
  );
});

/* -------------------------------------------------------------- 行内语法 */

check("加粗 / 斜体 / 删除线 / 行内代码", (html) => {
  const out = renderMarkdown("**粗** *斜* ~~删~~ `code`");
  return (
    out.includes("<strong>粗</strong>") &&
    out.includes("<em>斜</em>") &&
    out.includes("<del>删</del>") &&
    out.includes("<code>code</code>")
  );
});

check("行内代码内的 * 不被当成斜体", (html) => {
  const out = renderMarkdown("用 `a*b*c` 表示乘积");
  return out.includes("<code>a*b*c</code>") && !out.includes("<em>");
});

check("安全链接与自动链接", (html) => {
  const out = renderMarkdown("[官网](https://example.com) 和 https://example.org/x");
  return (
    out.includes('href="https://example.com"') &&
    out.includes('rel="noopener noreferrer"') &&
    out.includes('href="https://example.org/x"')
  );
});

check("图片使用安全属性", (html) => {
  const out = renderMarkdown("![示意图](https://example.com/a.png)");
  return out.includes("<img") && out.includes('referrerpolicy="no-referrer"');
});

/* ---------------------------------------------------------------- 安全性 */

check("脚本标签被转义", () => {
  const out = renderMarkdown("<script>alert(1)</script>");
  return !out.includes("<script>") && out.includes("&lt;script&gt;");
});

check("属性注入被转义", () => {
  const out = renderMarkdown('<img src=x onerror=alert(1)>');
  return !out.includes("<img src=x") && out.includes("&lt;img");
});

check("表格单元格内 XSS 被转义", () => {
  const out = renderMarkdown("| a | b |\n| --- | --- |\n| <script>x</script> | y |");
  return !out.includes("<script>") && out.includes("&lt;script&gt;");
});

check("javascript: 链接降级为纯文本", () => {
  const out = renderMarkdown("[点我](javascript:alert(1))");
  return !out.includes("javascript:") && out.includes("点我");
});

check("javascript: 图片被拒绝", () => {
  const out = renderMarkdown("![x](javascript:alert(1))");
  return !out.includes("<img");
});

/* --------------------------------------------------------- 流式/边界情况 */

check("未闭合代码块也能渲染（流式输出中）", () => {
  const out = renderMarkdown("说明：\n\n```python\nprint('hi')");
  return out.includes('class="language-python"');
});

check("空输入返回空串", () => renderMarkdown("") === "" && renderMarkdown(null) === "");

/* ------------------------------------------------------------------ 执行 */

let failed = 0;
for (const item of cases) {
  let ok = false;
  try {
    ok = Boolean(item.fn());
  } catch (error) {
    console.log(`FAIL ${item.name} -> ${error.message}`);
    failed += 1;
    continue;
  }
  if (ok) {
    console.log(`ok   ${item.name}`);
  } else {
    console.log(`FAIL ${item.name}`);
    failed += 1;
  }
}

console.log(`\n${cases.length - failed}/${cases.length} 通过`);
process.exit(failed ? 1 : 0);
