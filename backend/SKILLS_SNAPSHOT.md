<available_skills>
  <skill>
    <name>dialogue-summarizer</name>
    <description>智能总结对话上下文，提取关键信息、识别行动项、生成结构化回顾。当用户需要总结对话、复盘讨论、提取关键点、生成会议纪要、识别行动项或进行对话回顾时，立即使用此技能。即使对话很长或上下文不完整，也要使用此技能来提供有价值的总结。</description>
    <location>./backend/skills/dialogue-summarizer/SKILL.md</location>
  </skill>
  <skill>
    <name>get-weather</name>
    <description>Fetch real-time weather information for specified cities including temperature, humidity, wind speed, and conditions. Use when asked about current weather, temperature, humidity, or wind conditions for any city.</description>
    <location>./backend/skills/get-weather/SKILL.md</location>
  </skill>
  <skill>
    <name>get_date</name>
    <description>获取当前日期和时间信息。当用户询问日期、时间、今天是星期几、当前时间、现在几点了、今天是几月几号、现在是哪一年、或者任何与当前日期时间相关的问题时，立即使用此技能。即使用户只是简单地问"现在几点了？"或"今天星期几？"，也要使用此技能来提供准确的日期时间信息。</description>
    <location>./backend/skills/get_date/SKILL.md</location>
  </skill>
  <skill>
    <name>get-date-optimized</name>
    <description>['State what this skill does and when to use it. Include "Use when..." and representative request patterns.']</description>
    <location>./backend/skills/get-date-optimized/SKILL.md</location>
  </skill>
  <skill>
    <name>get-date</name>
    <description>获取当前系统日期和时间信息。当用户询问日期、时间、今天是星期几、当前时间、现在几点了、今天是几月几号、现在是哪一年、或者任何与当前日期时间相关的问题时，立即使用此技能。即使用户只是简单地问"现在几点了？"或"今天星期几？"，也要使用此技能来提供准确的日期时间信息。Use when asked about current date, time, weekday, month, year, or any time-related queries.</description>
    <location>./backend/skills/get_date_v2/SKILL.md</location>
  </skill>
  <skill>
    <name>get_weather</name>
    <description>获取指定城市的实时天气信息。当用户询问天气、气温、湿度、风力、天气预报、气候状况、穿衣建议、出行建议、或者任何与天气相关的问题时，立即使用此技能。即使用户只是简单地问"今天天气怎么样？"或"外面冷吗？"，也要使用此技能来提供准确的天气信息。支持全球主要城市，包括中文城市名和英文城市名。</description>
    <location>./backend/skills/get_weather/SKILL.md</location>
  </skill>
  <skill>
    <name>skill-benchmark</name>
    <description>Use when asked to evaluate whether a local or external skill is actually effective across representative prompts, multiple models, and baseline-vs-with-skill comparisons. Supports quick checks, formal benchmark runs, skill-vs-skill comparisons, and long-term trend reviews.
</description>
    <location>./backend/skills/skill-benchmark/SKILL.md</location>
  </skill>
  <skill>
    <name>skill-creator</name>
    <description>Create new skills, modify and improve existing skills, and measure skill performance. Use when users want to create a skill from scratch, edit, or optimize an existing skill, run evals to test a skill, benchmark skill performance with variance analysis, or optimize a skill's description for better triggering accuracy.</description>
    <location>./backend/skills/skill-creator/SKILL.md</location>
  </skill>
  <skill>
    <name>skill-creator-pro</name>
    <description>Design, create, review, and iteratively improve high-quality AI skills with strong trigger definitions, progressive disclosure, reusable scripts/references/assets planning, validation rules, and anti-pattern avoidance. Use when asked to create a new skill, upgrade an existing skill, turn a repeated workflow into a reusable skill, review skill quality, or define skill design best practices.</description>
    <location>./backend/skills/skill-creator-pro/SKILL.md</location>
  </skill>
  <skill>
    <name>example-skill</name>
    <description>['Describe what this skill does and when to use it. Include representative requests and boundaries.']</description>
    <location>./backend/skills/skill-template/SKILL.md</location>
  </skill>
</available_skills>