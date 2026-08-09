FROM node:22-alpine

WORKDIR /app

# Dependency layer first so source edits don't reinstall node_modules.
COPY package.json package-lock.json* ./
RUN npm install

COPY . .

EXPOSE 3000

CMD ["npm", "run", "dev"]
