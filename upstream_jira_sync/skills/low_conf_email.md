---
name: Low Confidence Email
description: Subject and body for the email sent when the bot cannot track a PR
---

Subject: upstream-jira-sync could not track your PR #{pr_number}

Hi @{github},

The sync bot could not confidently link your PR to a Jira ticket:
  {pr_title}
  {pr_url}

Please create the ticket manually and link the PR URL on it; the bot will pick it up and track it automatically from the next run.
