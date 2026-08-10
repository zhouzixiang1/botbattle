import * as React from "react"

import { cn } from "@/lib/utils"

type CardDensity = "default" | "compact"

const CardDensityContext = React.createContext<CardDensity>("default")

function Card({
  className,
  density = "default",
  children,
  ...props
}: React.ComponentProps<"div"> & { density?: CardDensity }) {
  return (
    <CardDensityContext.Provider value={density}>
      <div
        data-slot="card"
        data-density={density}
        className={cn(
          "flex min-w-0 flex-col rounded-xl border bg-card text-card-foreground shadow-sm",
          density === "compact" ? "gap-2 py-3" : "gap-3 py-4",
          className
        )}
        {...props}
      >
        {children}
      </div>
    </CardDensityContext.Provider>
  )
}

function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  const density = React.useContext(CardDensityContext)
  return (
    <div
      data-slot="card-header"
      className={cn(
        "@container/card-header grid min-w-0 auto-rows-min grid-rows-[auto_auto] items-start has-data-[slot=card-action]:grid-cols-[minmax(0,1fr)_auto]",
        density === "compact" ? "gap-1 px-3 [.border-b]:pb-3" : "gap-1.5 px-4 [.border-b]:pb-4",
        className
      )}
      {...props}
    />
  )
}

function CardTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-title"
      className={cn("leading-none font-semibold", className)}
      {...props}
    />
  )
}

function CardDescription({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-description"
      className={cn("text-sm text-muted-foreground", className)}
      {...props}
    />
  )
}

function CardAction({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-action"
      className={cn(
        "col-start-2 row-span-2 row-start-1 self-start justify-self-end",
        className
      )}
      {...props}
    />
  )
}

function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  const density = React.useContext(CardDensityContext)
  return (
    <div
      data-slot="card-content"
      className={cn("min-w-0", density === "compact" ? "px-3" : "px-4", className)}
      {...props}
    />
  )
}

function CardFooter({ className, ...props }: React.ComponentProps<"div">) {
  const density = React.useContext(CardDensityContext)
  return (
    <div
      data-slot="card-footer"
      className={cn(
        "flex min-w-0 items-center",
        density === "compact" ? "px-3 [.border-t]:pt-3" : "px-4 [.border-t]:pt-4",
        className
      )}
      {...props}
    />
  )
}

export {
  Card,
  CardHeader,
  CardFooter,
  CardTitle,
  CardAction,
  CardDescription,
  CardContent,
}
export type { CardDensity }
