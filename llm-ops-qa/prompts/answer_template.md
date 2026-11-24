<!--
  回答拼装模板：{{xxx}} 形式的占位符由 src/generator.py 的 render_template() 替换。
  可用占位符：{{mode}} / {{query}} / {{answer}} / {{references}}
  这个模板同时用于离线检索模式和 LLM 生成模式，保证两种模式输出结构一致。
-->
> 模式：{{mode}}

### 问题

{{query}}

### 回答

{{answer}}

### 参考

{{references}}
