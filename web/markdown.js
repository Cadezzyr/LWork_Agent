/*!
 * Markdown 渲染器（无第三方依赖，离线可用）
 *
 * 支持：标题、段落、软换行、无序/有序列表（含一级嵌套、任务清单）、
 *       GFM 表格（含左/中/右对齐）、引用块、分割线、围栏代码块（带语言标签+复制）、
 *       行内代码、加粗、斜体、删除线、链接、自动链接、图片。
 *
 * 安全：先做 HTML 转义再拼标签，模型输出里的 <script> / <img onerror> 只会显示成文本；
 *       链接只允许 http/https/mailto/相对路径，图片只允许 http/https/相对路径。
 *
 * 同时导出到 window.renderMarkdown 与 module.exports，便于在 Node 里做单元测试。
 */
(function (global) {
  "use strict";

  var CODE_PLACEHOLDER = "\u0000INLINECODE";
  var BLOCK_PLACEHOLDER = "\u0000CODEBLOCK";

  function escapeHtml(input) {
    return String(input)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function unescapeAmp(value) {
    return String(value).replace(/&amp;/g, "&");
  }

  function isSafeUrl(url) {
    var value = unescapeAmp(String(url)).trim();
    if (!value) return false;
    if (/^(\/|#|\.{1,2}\/)/.test(value)) return true;
    return /^(https?:\/\/|mailto:)/i.test(value);
  }

  /* ------------------------------------------------------------- 行内元素 */

  function renderInline(escapedText) {
    var out = escapedText;

    // 行内代码先抽出来，避免里面的 * _ [ ] 被当成 Markdown 语法
    var codes = [];
    out = out.replace(/`([^`\n]+)`/g, function (_, code) {
      codes.push(code);
      return CODE_PLACEHOLDER + (codes.length - 1) + "\u0000";
    });

    // 图片 ![alt](src)
    out = out.replace(/!\[([^\]\n]*)\]\(([^)\s]+)\)/g, function (whole, alt, src) {
      if (!isSafeUrl(src)) return alt;
      return (
        '<img src="' + unescapeAmp(src) + '" alt="' + escapeHtml(unescapeAmp(alt)) + '"' +
        ' loading="lazy" referrerpolicy="no-referrer" />'
      );
    });

    // 链接 [text](href)
    out = out.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, function (whole, label, href) {
      if (!isSafeUrl(href)) return label;
      return (
        '<a href="' + unescapeAmp(href) + '" target="_blank" rel="noopener noreferrer">' +
        label +
        "</a>"
      );
    });

    // 裸链接自动转 <a>
    out = out.replace(/(^|[\s(（])(https?:\/\/[^\s<)）]+)/g, function (_, lead, url) {
      return lead + '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + "</a>";
    });

    // 强调
    out = out.replace(/\*\*\*([^*\n]+)\*\*\*/g, "<strong><em>$1</em></strong>");
    out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^_\w])_([^_\n]+)_(?![_\w])/g, "$1<em>$2</em>");
    out = out.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");

    // 还原行内代码
    var codePattern = new RegExp(CODE_PLACEHOLDER + "(\\d+)\\u0000", "g");
    out = out.replace(codePattern, function (_, index) {
      return "<code>" + codes[Number(index)] + "</code>";
    });

    return out;
  }

  /* --------------------------------------------------------------- 代码块 */

  function renderCodeBlock(lang, code) {
    var escapedCode = escapeHtml(code);
    var cls = lang ? ' class="language-' + escapeHtml(lang) + '"' : "";
    var label = lang ? escapeHtml(lang) : "code";
    return (
      '<div class="code-block" data-lang="' + label + '">' +
      '<button class="code-copy" type="button" data-copy="' + escapedCode + '">复制</button>' +
      "<pre><code" + cls + ">" + escapedCode + "</code></pre>" +
      "</div>"
    );
  }

  /* ----------------------------------------------------------------- 表格 */

  function splitTableRow(line) {
    var trimmed = line.trim();
    if (trimmed.charAt(0) === "|") trimmed = trimmed.slice(1);
    if (trimmed.charAt(trimmed.length - 1) === "|") trimmed = trimmed.slice(0, -1);
    return trimmed.split("|").map(function (cell) {
      return cell.trim();
    });
  }

  function isAlignmentRow(line) {
    if (line.indexOf("-") === -1) return false;
    var cells = splitTableRow(line);
    if (!cells.length) return false;
    return cells.every(function (cell) {
      return /^:?-{2,}:?$/.test(cell.replace(/\s/g, ""));
    });
  }

  function alignmentOf(cell) {
    var value = cell.replace(/\s/g, "");
    var left = value.charAt(0) === ":";
    var right = value.charAt(value.length - 1) === ":";
    if (left && right) return "center";
    if (right) return "right";
    if (left) return "left";
    return "";
  }

  function renderTable(headerCells, alignments, bodyRows) {
    var parts = ['<div class="table-wrap"><table><thead><tr>'];
    headerCells.forEach(function (cell, index) {
      var align = alignments[index] ? ' style="text-align:' + alignments[index] + '"' : "";
      parts.push("<th" + align + ">" + renderInline(escapeHtml(cell)) + "</th>");
    });
    parts.push("</tr></thead><tbody>");
    bodyRows.forEach(function (row) {
      parts.push("<tr>");
      for (var index = 0; index < headerCells.length; index++) {
        var align = alignments[index] ? ' style="text-align:' + alignments[index] + '"' : "";
        var value = row[index] === undefined ? "" : row[index];
        parts.push("<td" + align + ">" + renderInline(escapeHtml(value)) + "</td>");
      }
      parts.push("</tr>");
    });
    parts.push("</tbody></table></div>");
    return parts.join("");
  }

  /* ----------------------------------------------------------------- 列表 */

  function matchListItem(line) {
    var match = /^([ \t]*)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/.exec(line);
    if (!match) return null;
    var indent = match[1].replace(/\t/g, "    ").length;
    return {
      indent: indent,
      ordered: /^\d/.test(match[2]),
      text: match[3],
    };
  }

  function renderListItems(items) {
    var cursor = { pos: 0 };
    var html = [];
    while (cursor.pos < items.length) {
      html.push(renderListLevel(items, cursor, items[cursor.pos].indent));
    }
    return html.join("");
  }

  function renderListLevel(items, cursor, indent) {
    var tag = items[cursor.pos].ordered ? "ol" : "ul";
    var html = ["<" + tag + ">"];
    while (cursor.pos < items.length) {
      var item = items[cursor.pos];
      if (item.indent < indent) break;
      if (item.indent > indent) {
        // 缩进跳跃（没有父级），当成同级处理，避免死循环
        html.push(renderListLevel(items, cursor, item.indent));
        continue;
      }
      var itemTag = item.ordered ? "ol" : "ul";
      if (itemTag !== tag) break;
      cursor.pos += 1;

      var body = "";
      var task = /^\[([ xX])\][ \t]+([\s\S]*)$/.exec(item.text);
      if (task) {
        var checked = task[1].toLowerCase() === "x";
        body =
          '<span class="task' + (checked ? " done" : "") + '">' +
          (checked ? "☑" : "☐") +
          "</span>" +
          renderInline(escapeHtml(task[2]));
      } else {
        body = renderInline(escapeHtml(item.text));
      }

      var nested = "";
      if (cursor.pos < items.length && items[cursor.pos].indent > indent) {
        nested = renderListLevel(items, cursor, items[cursor.pos].indent);
      }
      html.push("<li>" + body + nested + "</li>");
    }
    html.push("</" + tag + ">");
    return html.join("");
  }

  /* ------------------------------------------------------------- 主渲染器 */

  function renderMarkdown(source) {
    if (source === null || source === undefined) return "";
    var text = String(source).replace(/\r\n?/g, "\n").replace(/\t/g, "    ");

    // 1) 先把围栏代码块整段抽走，避免其中的符号被当成 Markdown
    var codeBlocks = [];
    text = text.replace(/```([\w+#.\- ]*)\n([\s\S]*?)(?:```|$)/g, function (_, lang, code) {
      var language = String(lang || "").trim().split(/\s+/)[0];
      codeBlocks.push(renderCodeBlock(language, code.replace(/\n$/, "")));
      return BLOCK_PLACEHOLDER + (codeBlocks.length - 1) + "\u0000\n";
    });

    var lines = text.split("\n");
    var html = [];
    var index = 0;

    while (index < lines.length) {
      var line = lines[index];
      var trimmed = line.trim();

      // 空行
      if (!trimmed) {
        index += 1;
        continue;
      }

      // 代码块占位符
      var placeholder = /^\u0000CODEBLOCK(\d+)\u0000$/.exec(trimmed);
      if (placeholder) {
        html.push(codeBlocks[Number(placeholder[1])]);
        index += 1;
        continue;
      }

      // 标题
      var heading = /^(#{1,6})\s+(.*)$/.exec(trimmed);
      if (heading) {
        var level = heading[1].length;
        html.push(
          "<h" + level + ">" + renderInline(escapeHtml(heading[2].replace(/\s+#+\s*$/, ""))) + "</h" + level + ">"
        );
        index += 1;
        continue;
      }

      // 分割线（表格对齐行由表格分支处理，这里不会误伤）
      if (/^(\*\s*){3,}$/.test(trimmed) || /^(-\s*){3,}$/.test(trimmed) || /^(_\s*){3,}$/.test(trimmed)) {
        html.push("<hr>");
        index += 1;
        continue;
      }

      // 引用块（连续 > 行合并为一个 blockquote）
      if (trimmed.charAt(0) === ">") {
        var quoteLines = [];
        while (index < lines.length) {
          var current = lines[index].trim();
          if (current.charAt(0) !== ">") break;
          quoteLines.push(current.replace(/^>\s?/, ""));
          index += 1;
        }
        html.push("<blockquote>" + renderInline(escapeHtml(quoteLines.join("\n"))).replace(/\n/g, "<br>") + "</blockquote>");
        continue;
      }

      // 表格：当前行含 |，且下一行是 |---| 对齐行
      if (
        trimmed.indexOf("|") !== -1 &&
        index + 1 < lines.length &&
        isAlignmentRow(lines[index + 1])
      ) {
        var headerCells = splitTableRow(trimmed);
        var alignments = splitTableRow(lines[index + 1]).map(alignmentOf);
        index += 2;
        var bodyRows = [];
        while (index < lines.length) {
          var rowLine = lines[index].trim();
          if (!rowLine || rowLine.indexOf("|") === -1) break;
          if (/^(\*\s*){3,}$/.test(rowLine) || /^(-\s*){3,}$/.test(rowLine)) break;
          bodyRows.push(splitTableRow(rowLine));
          index += 1;
        }
        html.push(renderTable(headerCells, alignments, bodyRows));
        continue;
      }

      // 列表
      if (matchListItem(line)) {
        var items = [];
        while (index < lines.length) {
          var item = matchListItem(lines[index]);
          if (item) {
            items.push(item);
            index += 1;
            continue;
          }
          // 列表项的续行（缩进 2 空格以上、非空行）
          if (
            lines[index].trim() &&
            /^\s{2,}\S/.test(lines[index]) &&
            items.length &&
            lines[index].trim().charAt(0) !== "|"
          ) {
            items[items.length - 1].text += "\n" + lines[index].trim();
            index += 1;
            continue;
          }
          break;
        }
        html.push(renderListItems(items));
        continue;
      }

      // 普通段落：连续非空、且不是其它块起始的行
      var paragraph = [trimmed];
      index += 1;
      while (index < lines.length) {
        var next = lines[index];
        var nextTrimmed = next.trim();
        if (!nextTrimmed) break;
        if (/^\u0000CODEBLOCK\d+\u0000$/.test(nextTrimmed)) break;
        if (/^(#{1,6})\s+/.test(nextTrimmed)) break;
        if (nextTrimmed.charAt(0) === ">") break;
        if (matchListItem(next)) break;
        if (
          nextTrimmed.indexOf("|") !== -1 &&
          index + 1 < lines.length &&
          isAlignmentRow(lines[index + 1])
        ) {
          break;
        }
        paragraph.push(nextTrimmed);
        index += 1;
      }
      html.push("<p>" + paragraph.map(function (row) { return renderInline(escapeHtml(row)); }).join("<br>") + "</p>");
    }

    return html.join("\n");
  }

  var api = { renderMarkdown: renderMarkdown, escapeHtml: escapeHtml, renderInline: renderInline };
  global.renderMarkdown = renderMarkdown;
  global.escapeHtml = escapeHtml;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
