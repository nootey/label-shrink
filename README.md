# LabelShrink

Simple discord bot to automate shrinking shipping labels to a4 format, so that they can be printed easily.

## Running app locally

Before you start, you'll need to install [NodeJS](https://nodejs.org/en/download/) and 

[create a Discord app](https://discord.com/developers/applications) with the proper permissions:

- `bot`
- `send.messages`
- `send.messages.threads`
- `attach.files`
- `read.message.history`

And generate an OAuth2 invite link. The bot will then auto respond to any pdf files, or to messages with a link to a hosted pdf (the bot checks the link's response headers, not just the file extension). No scoping is currently implemented.

## Docker

- A dockerfile is provided for easy deployment.
- `make start`

Configuring the app is covered in detail in the [getting started guide](https://discord.com/developers/docs/getting-started).
